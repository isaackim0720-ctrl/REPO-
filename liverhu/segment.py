"""Liver + Couinaud segment segmentation with TotalSegmentator models, run in-process.

Why not just call TotalSegmentator()? Its default pipeline starts several worker
processes (each loading PyTorch) and keeps full-resolution copies of the scan; on a
large CT that peaked above 7 GB RAM, too much for 8 GB lab PCs. Here we:
  1. shrink the scan to ~6 mm by block-averaging (cheap, chunked) and run the
     6 mm TotalSegmentator model only to find where the liver is;
  2. crop the original-resolution scan to the liver (+ margin);
  3. run the TotalSegmentator `liver_segments` model (Couinaud 1-8) on that crop,
     in this process, with no worker processes;
  4. likewise run the 1.5 mm organs model on a spleen crop to outline the spleen.
Each model is freed before the next one is loaded.
Models: TotalSegmentator (Wasserthal et al., Radiology: AI 2023), trained on real CTs.
"""
from __future__ import annotations

import math
import os
import time

import numpy as np

LIVER_SEGMENTS_TASK = 570
LIVER_SEGMENTS_TRAINER = "nnUNetTrainerNoMirroring"
COARSE_TASK = 298                       # TotalSegmentator "total" 6 mm model
COARSE_TRAINER = "nnUNetTrainer_4000epochs_NoMirroring"
COARSE_LIVER_LABEL = 5
COARSE_SPLEEN_LABEL = 1
COARSE_SPACING_MM = 6.0
ORGANS_TASK = 291                       # TotalSegmentator "total" 1.5 mm organs model (spleen = 1)
ORGANS_TRAINER = "nnUNetTrainerNoMirroring"
ORGANS_SPLEEN_LABEL = 1
ORGANS_SPACING_MM = [1.5, 1.5, 1.5]
REQUIRED_MODELS = (COARSE_TASK, LIVER_SEGMENTS_TASK, ORGANS_TASK)

_PREDICTORS: dict = {}
# Tile sizes. Default = each model's own (liver segments 64 x 192 x 192, organs 128^3).
# LIVERHU_LOW_MEMORY=1 (start with --low-memory) uses smaller tiles and at most 4 CPU
# threads: lower peak RAM for 8 GB computers; segment borders differ very slightly
# (Dice ~0.96-0.99), so use one setting for a whole study (recorded as model_tile).
DEFAULT_TILE = [64, 192, 192]
LOW_MEMORY_TILE = [int(v) for v in os.environ.get("LIVERHU_LOW_TILE", "64,160,160").split(",")]
LOW_MEMORY_ORGANS_TILE = [int(v) for v in os.environ.get("LIVERHU_LOW_ORGANS_TILE", "96,96,96").split(",")]
LOW_MEMORY = os.environ.get("LIVERHU_LOW_MEMORY", "").lower() in ("1", "true", "yes")
PATCH_OVERRIDE: dict = {}
if LOW_MEMORY:
    PATCH_OVERRIDE[LIVER_SEGMENTS_TASK] = LOW_MEMORY_TILE
    PATCH_OVERRIDE[ORGANS_TASK] = LOW_MEMORY_ORGANS_TILE


def release_memory() -> None:
    """Free unused Python objects and ask the C allocator to hand freed memory back to
    the operating system (otherwise each stage starts from the previous stage's peak)."""
    import ctypes
    import gc
    import sys
    gc.collect()
    try:
        if sys.platform.startswith("linux"):
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        elif sys.platform == "darwin":
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            libc.malloc_zone_pressure_relief.restype = ctypes.c_size_t
            libc.malloc_zone_pressure_relief(None, ctypes.c_size_t(0))
    except Exception:
        pass


def tile_in_use() -> str:
    t = "x".join(str(v) for v in PATCH_OVERRIDE.get(LIVER_SEGMENTS_TASK, DEFAULT_TILE))
    return t + (" (low-memory)" if LOW_MEMORY else "")


def _device():
    import torch
    if torch.cuda.is_available():
        return torch.device("cuda")
    n = max(1, os.cpu_count() or 1)
    torch.set_num_threads(min(n, 4) if LOW_MEMORY else n)
    return torch.device("cpu")


def download_models() -> None:
    from totalsegmentator.config import setup_nnunet, setup_totalseg, set_config_key
    from totalsegmentator.libs import download_pretrained_weights
    setup_nnunet()
    setup_totalseg()
    set_config_key("send_usage_stats", False)
    for task_id in REQUIRED_MODELS:
        download_pretrained_weights(task_id)


def _predictor(task_id: int, trainer: str, step_size: float):
    key = (task_id, trainer)
    if key in _PREDICTORS:
        return _PREDICTORS[key]
    from totalsegmentator.config import setup_nnunet
    setup_nnunet()
    import totalsegmentator.nnunet  # noqa: F401  (registers TotalSegmentator's custom trainer classes)
    from totalsegmentator.libs import download_pretrained_weights
    from nnunetv2.inference.predict_from_raw_data import nnUNetPredictor
    from nnunetv2.utilities.file_path_utilities import get_output_folder
    download_pretrained_weights(task_id)          # no-op when already present
    folder = get_output_folder(task_id, trainer, "nnUNetPlans", "3d_fullres")
    pred = nnUNetPredictor(tile_step_size=step_size, use_gaussian=True, use_mirroring=False,
                           perform_everything_on_device=True, device=_device(),
                           verbose=False, verbose_preprocessing=False, allow_tqdm=False)
    pred.initialize_from_trained_model_folder(folder, use_folds=(0,), checkpoint_name="checkpoint_final.pth")
    # nnU-Net keeps a second full copy of the weights (one per fold) to reload before each
    # prediction. With a single fold, point it at the network's own tensors instead
    # (identical values, ~350 MB less RAM).
    if len(pred.list_of_parameters) == 1:
        pred.list_of_parameters = [pred.network.state_dict()]
        import gc
        gc.collect()
    patch = PATCH_OVERRIDE.get(task_id)
    if patch:   # smaller sliding-window tiles -> lower peak RAM (same network, same weights)
        pred.configuration_manager.configuration["patch_size"] = list(patch)
    _PREDICTORS[key] = pred
    return pred


def _predict(pred, vol_xyz: np.ndarray, spacing_xyz) -> np.ndarray:
    """vol in canonical (x, y, z) order -> label map in the same order/shape."""
    data = np.ascontiguousarray(vol_xyz.transpose(2, 1, 0)[None], dtype=np.float32)
    props = {"spacing": [float(spacing_xyz[2]), float(spacing_xyz[1]), float(spacing_xyz[0])]}
    seg = pred.predict_single_npy_array(data, props, None, None, False)
    return np.asarray(seg, dtype=np.uint8).transpose(2, 1, 0)


def block_mean(vol: np.ndarray, factors) -> np.ndarray:
    """Average-pool vol by integer factors, processing a few slices at a time."""
    fx, fy, fz = factors
    X, Y, Z = (s // f for s, f in zip(vol.shape, factors))
    out = np.empty((X, Y, Z), np.float32)
    for k in range(Z):
        blk = np.asarray(vol[:X * fx, :Y * fy, k * fz:(k + 1) * fz], dtype=np.float32)
        out[:, :, k] = blk.reshape(X, fx, Y, fy, fz).mean(axis=(1, 3, 4))
    return out


def largest_component(mask: np.ndarray) -> np.ndarray:
    from scipy import ndimage
    lab, n = ndimage.label(mask)
    if n > 1:
        sizes = ndimage.sum(mask, lab, range(1, n + 1))
        mask = lab == (int(np.argmax(sizes)) + 1)
    return mask


def _box_from_coarse(mask_small, factors, spacing, shape, margin_mm):
    if mask_small.sum() < 8:
        return None
    idx = np.nonzero(largest_component(mask_small))
    box = []
    for ax in range(3):
        lo = idx[ax].min() * factors[ax]
        hi = (idx[ax].max() + 1) * factors[ax]
        m = int(math.ceil(margin_mm / spacing[ax]))
        box.append(slice(int(max(0, lo - m)), int(min(shape[ax], hi + m))))
    return tuple(box)


def locate_organs(vol: np.ndarray, spacing, margin_mm: float = 20.0) -> dict:
    """Bounding boxes {"liver": ..., "spleen": ...} (tuples of slices or None) from
    the 6 mm model run on a block-averaged copy of the scan."""
    factors = [max(1, int(round(COARSE_SPACING_MM / s))) for s in spacing]
    small = block_mean(vol, factors)
    small_spacing = [s * f for s, f in zip(spacing, factors)]
    pred = _predictor(COARSE_TASK, COARSE_TRAINER, step_size=0.8)
    coarse = _predict(pred, small, small_spacing)
    liver = coarse == COARSE_LIVER_LABEL
    return {
        "liver": _box_from_coarse(liver, factors, spacing, vol.shape, margin_mm) if liver.sum() >= 20 else None,
        "spleen": _box_from_coarse(coarse == COARSE_SPLEEN_LABEL, factors, spacing, vol.shape, margin_mm),
    }


def locate_liver(vol: np.ndarray, spacing, margin_mm: float = 20.0):
    """Bounding box (tuple of slices) of the liver in vol, from the 6 mm model."""
    return locate_organs(vol, spacing, margin_mm)["liver"]


def _model_on_crop(crop: np.ndarray, spacing, task_id, trainer, target, step_size) -> np.ndarray:
    """Resample exactly as TotalSegmentator does (linear to the model's grid, integer
    HU), predict, then nearest-neighbour back to the crop grid."""
    import nibabel as nib
    from totalsegmentator.resampling import change_spacing
    aff = np.diag([float(spacing[0]), float(spacing[1]), float(spacing[2]), 1.0])
    img = nib.Nifti1Image(crop.astype(np.float32), aff)
    rsp = change_spacing(img, target, order=1, dtype=np.int32, nr_cpus=1)
    data = np.asanyarray(rsp.dataobj)
    del img
    pred = _predictor(task_id, trainer, step_size=step_size)
    seg_rsp = _predict(pred, data, [float(z) for z in rsp.header.get_zooms()[:3]])
    del data
    release_memory()
    back = change_spacing(nib.Nifti1Image(seg_rsp, rsp.affine), target, target_shape=crop.shape,
                          order=0, dtype=np.uint8, nr_cpus=1, force_affine=aff)
    return np.asarray(back.dataobj, dtype=np.uint8)


def _segments_on_crop(crop: np.ndarray, spacing) -> np.ndarray:
    """Couinaud segments on a liver crop (TotalSegmentator liver_segments task)."""
    return _model_on_crop(crop, spacing, LIVER_SEGMENTS_TASK, LIVER_SEGMENTS_TRAINER,
                          _liver_segments_spacing(), step_size=0.5)


def _liver_segments_spacing():
    try:
        from totalsegmentator.map_tasks_config import TASK_CONFIGS  # type: ignore
        return list(TASK_CONFIGS["liver_segments"]["resample"])
    except Exception:
        return [0.8046879768371582, 0.8046879768371582, 1.5]


def segment_liver_and_spleen(vol: np.ndarray, spacing, log=print, spleen: bool = True):
    """Returns (segments, spleen_mask, info).

    segments: Couinaud labels (uint8, 0 = background, 1-8), same shape as vol.
    spleen_mask: bool array (same shape) or None if the spleen is not in the scan.
    vol: CT in HU, canonical RAS (x, y, z) order. spacing: voxel size (mm) per axis.
    """
    import gc
    t0 = time.time()
    info: dict = {"tile": tile_in_use()}
    boxes = locate_organs(vol, spacing)
    box = boxes["liver"]
    info["t_locate_s"] = round(time.time() - t0, 1)
    if box is None:
        raise RuntimeError("Could not find the liver in this scan (is it an abdominal CT?)")
    info["crop_shape"] = [int(b.stop - b.start) for b in box]
    log(f"liver located in {info['t_locate_s']} s; crop {info['crop_shape']}")
    _PREDICTORS.pop((COARSE_TASK, COARSE_TRAINER), None)   # free the coarse model
    release_memory()
    t1 = time.time()
    seg_crop = _segments_on_crop(np.asarray(vol[box]), spacing)
    seg = np.zeros(vol.shape, np.uint8)
    seg[box] = seg_crop
    del seg_crop
    info["t_segments_s"] = round(time.time() - t1, 1)
    log(f"liver segments done in {info['t_segments_s']} s")
    _PREDICTORS.pop((LIVER_SEGMENTS_TASK, LIVER_SEGMENTS_TRAINER), None)
    release_memory()

    spleen_mask = None
    sbox = boxes["spleen"] if spleen else None
    if spleen and sbox is None:
        info["spleen"] = "not found by the coarse model"
        log("spleen not found in this scan")
    elif sbox is not None:
        t2 = time.time()
        org = _model_on_crop(np.asarray(vol[sbox]), spacing, ORGANS_TASK, ORGANS_TRAINER,
                             ORGANS_SPACING_MM, step_size=0.8)
        sp = org == ORGANS_SPLEEN_LABEL
        del org
        if sp.sum() > 50:
            spleen_mask = np.zeros(vol.shape, bool)
            spleen_mask[sbox] = largest_component(sp)
        else:
            info["spleen"] = "not found by the organ model"
        info["t_spleen_s"] = round(time.time() - t2, 1)
        log(f"spleen done in {info['t_spleen_s']} s")
        _PREDICTORS.pop((ORGANS_TASK, ORGANS_TRAINER), None)
        release_memory()
    return seg, spleen_mask, info


def segment_liver(vol: np.ndarray, spacing, log=print) -> tuple[np.ndarray, dict]:
    """Couinaud segment labels only (no spleen)."""
    seg, _, info = segment_liver_and_spleen(vol, spacing, log=log, spleen=False)
    return seg, info

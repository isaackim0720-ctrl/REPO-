"""Test helpers: write small synthetic DICOM CT series."""
import datetime
from pathlib import Path

import numpy as np
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"


def write_series(vol_ras, spacing, out: Path, study_uid=None, desc="AXIAL", thickness=None,
                 contrast="", localizer=False, patient=("Test^Person", "PID123", "ACC9"), shuffle=False):
    """vol_ras: canonical (x->R, y->A, z->S) HU array. Writes one file per slice (no extension)."""
    out.mkdir(parents=True, exist_ok=True)
    X, Y, Z = vol_ras.shape
    sx, sy, sz = spacing
    study_uid = study_uid or generate_uid()
    series_uid = generate_uid()
    ks = [Z // 2] if localizer else list(range(Z))
    order = list(range(len(ks)))
    if shuffle:
        np.random.default_rng(0).shuffle(order)
    for n, k in enumerate(ks, 1):
        px = vol_ras[:, :, k][::-1, ::-1].T
        stored = np.clip(np.round(px) + 1024, 0, 4095).astype(np.uint16)
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CT_STORAGE
        meta.MediaStorageSOPInstanceUID = generate_uid()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        fname = out / f"IM{order[n - 1]:05d}"
        ds = FileDataset(str(fname), {}, file_meta=meta, preamble=b"\0" * 128)
        ds.SOPClassUID, ds.SOPInstanceUID = CT_STORAGE, meta.MediaStorageSOPInstanceUID
        ds.PatientName, ds.PatientID, ds.AccessionNumber = patient
        ds.StudyDate = datetime.date(2024, 1, 2).strftime("%Y%m%d")
        ds.Modality = "CT"
        ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.FrameOfReferenceUID = study_uid, series_uid, generate_uid()
        ds.SeriesDescription, ds.SeriesNumber, ds.InstanceNumber = desc, 2, n
        ds.ImageType = ["ORIGINAL", "PRIMARY", "LOCALIZER" if localizer else "AXIAL"]
        # world position of pixel (0,0): canonical voxel (X-1, Y-1, k) -> RAS -> LPS
        ras = np.array([(X - 1) * sx, (Y - 1) * sy, k * sz])
        ds.ImagePositionPatient = [-ras[0], -ras[1], ras[2]]
        ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        ds.PixelSpacing = [sy, sx]
        ds.SliceThickness = thickness or sz
        if contrast:
            ds.ContrastBolusAgent = contrast
        ds.Rows, ds.Columns = stored.shape
        ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
        ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
        ds.RescaleIntercept, ds.RescaleSlope = -1024, 1
        ds.PixelData = stored.tobytes()
        ds.save_as(fname, enforce_file_format=True)
    return study_uid

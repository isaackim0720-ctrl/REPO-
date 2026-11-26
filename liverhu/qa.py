"""Quality-check figure: each ROI on its axial slice + a coronal overview.

Images are shown in standard radiological convention: patient's RIGHT on the viewer's
LEFT, anterior at the top (axial) / superior at the top (coronal).
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Circle  # noqa: E402
import numpy as np  # noqa: E402

from .roi import LEFT_OF_LIGAMENT, RIGHT_LOBE, ligament_points  # noqa: E402

WINDOW = (-160, 240)          # abdomen window W400 / L40
COLORS = {"LL": "#00e5ff", "RL1": "#ff4dd2", "RL2": "#ffb000"}
REGION_STYLE = [((2, 3), "#00e5ff", "Seg 2/3 (left lobe)"),
                ((4,), "#9cff57", "Seg 4"),
                ((5, 6, 7, 8), "#ff4dd2", "Seg 5-8 (right lobe)"),
                ((1,), "#b0b0b0", "Seg 1 (caudate)")]


def _axial(arr2d):
    """canonical (x, y) slice -> display (rows = anterior->posterior, cols = pt right->left)."""
    return arr2d[::-1, ::-1].T


def _coronal(arr2d):
    """canonical (x, z) slice -> display (rows = superior->inferior, cols = pt right->left)."""
    return arr2d[::-1, ::-1].T


def _fmt(v, nd=1):
    return "-" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v:.{nd}f}"


def render_qa(ct, seg, rois, spacing, out_path: Path, title: str = "") -> None:
    sx, sy, sz = (float(s) for s in spacing)
    X, Y, Z = ct.shape
    fig, axes = plt.subplots(2, 2, figsize=(10.5, 10.5), facecolor="black")
    order = {"LL": (0, 0), "RL1": (0, 1), "RL2": (1, 0)}
    cols_mm = (np.arange(X)) * sx
    rows_mm = (np.arange(Y)) * sy

    for r in rois:
        ax = axes[order[r.key]]
        ax.set_facecolor("black")
        if not r.placed:
            ax.text(0.5, 0.5, f"{r.name}\nnot placed:\n{r.reason}", color="white", ha="center",
                    va="center", transform=ax.transAxes, fontsize=10)
            ax.axis("off")
            continue
        x, y, z = r.center_voxel
        img = _axial(ct[:, :, z])
        segd = _axial(seg[:, :, z])
        ax.imshow(img, cmap="gray", vmin=WINDOW[0], vmax=WINDOW[1], interpolation="nearest",
                  extent=(-0.5 * sx, (X - 0.5) * sx, (Y - 0.5) * sy, -0.5 * sy))
        for labels, color, _ in REGION_STYLE:
            m = np.isin(segd, labels)
            if m.any():
                ax.contour(cols_mm, rows_mm, m.astype(float), levels=[0.5], colors=color, linewidths=0.8)
        pts = ligament_points(seg[:, :, z])
        if len(pts):
            ax.scatter((X - 1 - pts[:, 0]) * sx, (Y - 1 - pts[:, 1]) * sy, s=7, c="yellow",
                       marker="s", linewidths=0, label="falciform ligament (est.)", zorder=5)
        cxm, cym = (X - 1 - x) * sx, (Y - 1 - y) * sy
        rad = r.diameter_mm / 2
        ax.add_patch(Circle((cxm, cym), rad, fill=False, ec=COLORS[r.key], lw=2.0))
        ax.text(cxm, cym - rad - 4, r.key, color=COLORS[r.key], ha="center", va="bottom",
                fontsize=11, weight="bold")
        liver = np.argwhere(segd > 0)
        r0, c0 = liver.min(axis=0)
        r1, c1 = liver.max(axis=0)
        pad = 15.0
        ax.set_xlim(c0 * sx - pad, c1 * sx + pad)
        ax.set_ylim(r1 * sy + pad, r0 * sy - pad)
        ax.text(0.01, 0.5, "R", color="white", transform=ax.transAxes, fontsize=12, va="center")
        ax.text(0.97, 0.5, "L", color="white", transform=ax.transAxes, fontsize=12, va="center")
        ax.text(0.5, 0.99, "A", color="white", transform=ax.transAxes, fontsize=12, ha="center", va="top")
        ax.set_title(f"{r.name} - axial z = {_fmt(r.z_mm)} mm"
                     + (f" (image {r.dicom_instance})" if getattr(r, "dicom_instance", None) else ""),
                     color="white", fontsize=10)
        txt = (f"mean {_fmt(r.mean_hu)}  SD {_fmt(r.sd_hu)}  min {_fmt(r.min_hu, 0)}  max {_fmt(r.max_hu, 0)} HU\n"
               f"area {_fmt(r.area_cm2, 2)} cm²  ({_fmt(r.diameter_mm, 0)} mm)   segment {r.segment_at_center}   "
               f"{'LEFT' if r.side_of_ligament == 'left' else 'RIGHT'} of ligament"
               + ("" if r.side_ok else "  ⚠ CHECK SIDE"))
        ax.text(0.5, -0.02, txt, color="white", transform=ax.transAxes, ha="center", va="top", fontsize=9)
        ax.set_xticks([]); ax.set_yticks([])

    # coronal overview through the liver centre, with the three ROI levels
    ax = axes[1, 1]
    ax.set_facecolor("black")
    liver3 = seg > 0
    j = int(np.round(np.nonzero(liver3.any(axis=(0, 2)))[0].mean())) if liver3.any() else Y // 2
    placed = [r for r in rois if r.placed]
    if placed:
        j = int(np.round(np.mean([r.center_voxel[1] for r in placed])))
    cor = _coronal(ct[:, j, :])
    ax.imshow(cor, cmap="gray", vmin=WINDOW[0], vmax=WINDOW[1], interpolation="nearest",
              extent=(-0.5 * sx, (X - 0.5) * sx, (Z - 0.5) * sz, -0.5 * sz), aspect="equal")
    zrows = np.arange(Z) * sz
    for labels, color, _ in REGION_STYLE:
        m = np.isin(_coronal(seg[:, j, :]), labels)
        if m.any():
            ax.contour(cols_mm, zrows, m.astype(float), levels=[0.5], colors=color, linewidths=0.8)
    for r in placed:
        x, _, z = r.center_voxel
        yrow = (Z - 1 - z) * sz
        ax.axhline(yrow, color=COLORS[r.key], lw=1.0, ls="--")
        ax.plot([(X - 1 - x) * sx], [yrow], "o", ms=7, mfc="none", mec=COLORS[r.key], mew=2)
        ax.text((X - 1 - x) * sx + 8, yrow - 3, r.key, color=COLORS[r.key], fontsize=10, weight="bold")
    if liver3.any():
        xs = np.nonzero(liver3.any(axis=(1, 2)))[0]
        zs = np.nonzero(liver3.any(axis=(0, 1)))[0]
        ax.set_xlim((X - 1 - xs.max()) * sx - 25, (X - 1 - xs.min()) * sx + 25)
        ax.set_ylim((Z - 1 - zs.min()) * sz + 25, (Z - 1 - zs.max()) * sz - 25)
    ax.text(0.01, 0.5, "R", color="white", transform=ax.transAxes, fontsize=12, va="center")
    ax.text(0.97, 0.5, "L", color="white", transform=ax.transAxes, fontsize=12, va="center")
    ax.text(0.5, 0.99, "S", color="white", transform=ax.transAxes, fontsize=12, ha="center", va="top")
    ax.set_title("Coronal overview - ROI levels", color="white", fontsize=10)
    ax.set_xticks([]); ax.set_yticks([])
    handles = [plt.Line2D([], [], color=c, lw=2, label=l) for _, c, l in REGION_STYLE]
    handles.append(plt.Line2D([], [], color="yellow", marker="s", ls="none", ms=4, label="falciform ligament (est.)"))
    fig.legend(handles=handles, loc="lower center", ncol=5, fontsize=8, frameon=False, labelcolor="white")
    fig.suptitle(title, color="white", fontsize=12)
    fig.subplots_adjust(left=0.02, right=0.98, top=0.94, bottom=0.07, wspace=0.05, hspace=0.22)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=100, facecolor="black")
    plt.close(fig)

import json
import zipfile

import nibabel as nib
import numpy as np

from liverhu.scanio import choose_series, discover_cases, load_case, safe_extract_zip
from tests.helpers import write_series


def _vol(shape=(40, 32, 30)):
    rng = np.random.default_rng(1)
    v = rng.normal(40, 10, shape).astype(np.float32)
    v[:, :, :3] = -1000          # some air so it looks like HU
    v[5, 7, 11] = 900            # marker voxel to check orientation
    return np.round(v)


def test_dicom_roundtrip_orientation_and_values(tmp_path):
    vol = _vol()
    write_series(vol, (0.8, 0.9, 2.0), tmp_path / "PatientX" / "series", shuffle=True)
    cases, warns = discover_cases(tmp_path)
    assert len(cases) == 1 and not warns
    img, meta = load_case(cases[0])
    a = np.asanyarray(img.dataobj)
    assert a.shape == vol.shape
    assert np.array_equal(a, vol.clip(-1024, 3071))
    assert np.allclose(img.header.get_zooms()[:3], (0.8, 0.9, 2.0), atol=1e-4)
    assert tuple(np.argwhere(a == 900)[0]) == (5, 7, 11)
    assert cases[0].case_id == "PatientX_series"


def test_series_choice_and_grouping_and_no_identifiers(tmp_path):
    vol = _vol()
    st = write_series(vol, (0.8, 0.8, 1.0), tmp_path / "A" / "thin", desc="THIN", thickness=1.0)
    write_series(vol, (0.8, 0.8, 3.0), tmp_path / "A" / "mid", study_uid=st, desc="MID", thickness=3.0)
    write_series(vol, (0.8, 0.8, 3.0), tmp_path / "A" / "scout", study_uid=st, desc="SCOUT", localizer=True)
    write_series(vol, (0.8, 0.8, 3.0), tmp_path / "B", desc="PV", thickness=3.0, contrast="OMNIPAQUE")
    cases, _ = discover_cases(tmp_path, preferred_slice_mm=3.0)
    by = {c.case_id: c for c in cases}
    assert set(by) == {"A", "B"}
    # uploaded as one top folder -> the shared top folder is left out of the names
    import shutil
    top = tmp_path.parent / (tmp_path.name + "_upload")
    shutil.copytree(tmp_path, top / "MyExport")
    assert sorted(c.case_id for c in discover_cases(top, label="cohort")[0]) == ["cohort_A", "cohort_B"]
    assert by["A"].meta["series_description"] == "MID"
    assert by["B"].meta["contrast_agent_tag"] == "OMNIPAQUE"
    blob = json.dumps([c.to_dict() for c in cases])
    assert "PID123" not in blob and "ACC9" not in blob and "Test^Person" not in blob
    cases_id, _ = discover_cases(tmp_path, include_identifiers=True)
    assert cases_id[0].meta["patient_id"] == "PID123"


def test_prefers_non_contrast():
    base = dict(modality="CT", axial=True, localizer=False, n_images=100, slice_thickness_mm=3.0)
    s = [dict(base, contrast_agent_tag="IV"), dict(base, contrast_agent_tag="", slice_thickness_mm=5.0)]
    assert choose_series(s, 3.0) == 1


def test_nifti_and_zip(tmp_path):
    vol = _vol()
    nib.save(nib.Nifti1Image(vol.astype(np.int16), np.diag([0.7, 0.7, 2.5, 1])), tmp_path / "scan1.nii.gz")
    d = tmp_path / "zipsrc"
    write_series(vol, (0.8, 0.8, 2.0), d)
    with zipfile.ZipFile(tmp_path / "study.zip", "w") as zf:
        for f in d.iterdir():
            zf.write(f, f"inner/{f.name}")
    import shutil
    shutil.rmtree(d)
    cases, _ = discover_cases(tmp_path)
    kinds = sorted(c.kind for c in cases)
    assert kinds == ["dicom", "nifti"]
    img, _ = load_case(next(c for c in cases if c.kind == "nifti"))
    assert img.shape == vol.shape


def test_public_dataset_layout_skips_labels_and_hidden_files(tmp_path):
    vol = _vol().astype(np.int16)
    aff = np.diag([0.8, 0.8, 2.0, 1])
    (tmp_path / "Task09_Spleen" / "imagesTr").mkdir(parents=True)
    (tmp_path / "Task09_Spleen" / "labelsTr").mkdir(parents=True)
    nib.save(nib.Nifti1Image(vol, aff), tmp_path / "Task09_Spleen" / "imagesTr" / "spleen_2.nii.gz")
    nib.save(nib.Nifti1Image((vol > 0).astype(np.uint8), aff), tmp_path / "Task09_Spleen" / "labelsTr" / "spleen_2.nii.gz")
    (tmp_path / "Task09_Spleen" / "imagesTr" / "._spleen_2.nii.gz").write_bytes(b"\0" * 10)
    cases, _ = discover_cases(tmp_path)
    assert [c.case_id for c in cases] == ["spleen_2"]
    assert "imagesTr" in cases[0].path


def test_zip_path_traversal_refused(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../escape.txt", "x")
    import pytest
    with pytest.raises(ValueError):
        safe_extract_zip(z, tmp_path / "out")
    assert not (tmp_path.parent / "escape.txt").exists()


def test_nifti_in_patient_folders_are_named_after_the_folder(tmp_path):
    vol = _vol().astype(np.int16)
    for pat in ("patient_101", "patient_102"):
        (tmp_path / "export" / pat).mkdir(parents=True)
        nib.save(nib.Nifti1Image(vol, np.diag([0.8, 0.8, 2.0, 1])), tmp_path / "export" / pat / "ct.nii.gz")
    cases, _ = discover_cases(tmp_path)
    assert [c.case_id for c in cases] == ["patient_101_ct", "patient_102_ct"]
    assert cases[0].study_files == [cases[0].path]

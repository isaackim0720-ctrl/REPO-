# Liver HU: automatic liver (3-circle) and spleen attenuation from CT

Upload a CT scan and get the Hounsfield-unit (HU) values a reader would measure with
circular ROIs, three in the liver and one in the spleen:

| ROI | Where | Rule |
|---|---|---|
| **LL** | Left lobe | inside Couinaud segments 2/3, **left of the falciform ligament** (≥ 10 mm from it) |
| **RL1** | Right lobe, upper | inside segments 7/8, right of the ligament |
| **RL2** | Right lobe, lower | inside segments 5/6, ≥ 30 mm below RL1 |
| **SPL** | Spleen | inside the spleen, middle part top to bottom, ≥ 5 mm from its edge |

For each circle: **mean, SD, min, max (and median) HU, area (cm²), diameter**, slice position
(z, mm) and DICOM image number, plus a **QA picture** showing where every circle went.
Also **liver − spleen** (liver attenuation index) and **liver / spleen** ratio, using the mean
of the 3 liver circles. Results download as Excel/CSV, one row per scan.

> Research use only, not for clinical diagnosis. Look at the QA image of every case.

## How it works

1. **Series choice**: per study, the axial CT series is picked automatically, preferring
   non-contrast and a slice thickness near 3 mm (configurable).
2. **AI outlining**: [TotalSegmentator](https://github.com/wasserth/TotalSegmentator)
   (trained on real CT scans) finds the liver with its 6 mm model, then outlines the
   8 Couinaud segments with its `liver_segments` model on the liver region only.
   The spleen is outlined by TotalSegmentator's 1.5 mm organ model, run on the spleen region only.
3. **Falciform ligament**: estimated on every slice as the boundary between segments 2/3
   and segment 4, the umbilical fissure where the ligament runs.
4. **Circle placement**: each circle sits fully inside its target segments, ≥ 5 mm from the
   liver/segment edge, on the slice and position where the tissue is most uniform (lowest SD
   and closest to that region's median HU). That steers it away from vessels, lesions and
   artefacts. If nothing fits, the rules are relaxed step by step and the case is flagged.
5. **Measurement** is done on the original scan pixels (no smoothing, no resampling).

Defaults (change on the website under *Advanced*, or in `liverhu/config.py`): circle diameter
15 mm (1.77 cm²), edge gap 5 mm, ligament gap 10 mm. Use the same settings for every scan in
a study; they are saved in every result row.

## Install (once, on the lab computer that will run the website)

Requirements: Windows 10/11, macOS or Linux; **8 GB RAM** (the AI step needs about 4 GB free,
so close other programs on this computer); about 6 GB disk; internet for the install.

**Windows**
1. Install Python from <https://www.python.org/downloads/> and tick **Add python.exe to PATH**.
   Any recent version works: if yours is too new for the medical-imaging packages (e.g. 3.15),
   the installer downloads a private Python 3.12 for this program and leaves yours unchanged.
2. Download this folder (on GitHub: *Code → Download ZIP*) and unzip it, e.g. to `C:\LiverHU`.
3. Double-click `scripts\install_windows.bat`, which takes 10–20 min.
4. Double-click `scripts\start_windows.bat`. The first time, it asks you to choose the website password.
   If Windows Firewall asks, allow access on **private networks**.

**macOS / Linux**: the `.bat` files are Windows-only. Install Python (step 1), download and
unzip (step 2; a Mac unzips automatically), then in Finder right-click the folder →
**New Terminal at Folder** and run `bash scripts/install_mac_linux.sh`, then
`bash scripts/start_mac_linux.sh`.

## Use it

* On the server computer open <http://localhost:8000>. Other lab members open
  `http://<server-address>:8000`; the start window prints the address (e.g. `http://192.168.1.23:8000`).
* Log in: **user name = your name** (it is recorded with your uploads), **password = the lab password**.
* Drag in a folder exported from your scan database (DICOM, any folder layout, many patients
  at once), `.zip` files, or `.nii/.nii.gz`. Each study is measured separately, one at a time,
  about 2–4 minutes per scan on a normal PC.
* Open the upload to watch progress, check the QA images, and download Excel/CSV.
  *All results (Excel)* in the top bar gives every scan ever measured.

The website is only reachable inside your lab network and only with the password.
Scans never leave the server computer and are **deleted right after they are measured**;
only the numbers and QA images are kept (in `liverhu_data/`). Start with `--keep-uploads` to
keep them. Patient names are never read; PatientID/AccessionNumber/StudyDate are added to
results only if you tick that option. Each study also gets a pseudonymous `study_key`.

Change the password: `python -m liverhu set-password` (with the server stopped).

## Batch mode (no website)

```
python -m liverhu process D:\exports\cohortA --out D:\results\cohortA
```
writes `liver_hu_results.xlsx/.csv` and one folder per case with `qa.png` and `result.json`.

## Output columns

`LL_mean_HU, LL_SD_HU, LL_min_HU, LL_max_HU, LL_area_cm2, LL_diameter_mm, LL_z_mm,
LL_image_no, LL_segment, LL_side_of_ligament` (same for `RL1_…`, `RL2_…` and the spleen
`SPL_…`), then `liver_mean_HU_3ROI`, `right_lobe_mean_HU`, `spleen_mean_HU`,
`liver_minus_spleen_HU`, `liver_spleen_ratio`, `liver_volume_mL`, `spleen_volume_mL`, `series_description`,
`slice_thickness_mm`, `kVp`, `contrast_agent_tag`, `study_key`, `flags`, settings and
software versions. SD is the sample SD (n − 1).

**Flags** mark cases to check: contrast-enhanced series, high SD, relaxed placement, possible
wrong side of the ligament, liver not fully in the scan, unusual liver volume.

## Before using it for the study: validate

Measure a subset (e.g. 30–50 scans) manually with your lab's protocol and compare with the
automatic values (intraclass correlation, Bland–Altman). If your protocol uses a different
circle size or slice rule, change the settings to match before running the full cohort.
HU cut-offs for fatty liver (e.g. ≤ 40 HU, or 48 HU) are for **non-contrast** CT.

## Memory / speed

| | normal scan (168 slices) | large scan (588 slices) |
|---|---|---|
| peak RAM | 4.0 GB | 4.3 GB |
| time (4 CPU cores) | ~2 min | ~1.5 min |

If the computer struggles, start with `--low-memory` (≈ 3.4 GB). It uses smaller AI tiles,
so segment borders shift very slightly; use one setting for a whole study (it is recorded
in the `model_tile` column). A computer with an NVIDIA GPU is used automatically and is much faster.

## Tests

`python -m pytest -q tests`. The tests check falciform-side and upper/lower placement on
synthetic livers, exact HU statistics, vessel/cyst avoidance, DICOM loading and orientation,
series choice, no identifiers in results by default, password protection and upload safety.

## References

* Wasserthal J et al. TotalSegmentator. *Radiology: AI* 2023. doi:10.1148/ryai.230024
* Kodama Y et al. Comparison of CT methods for determining the fat content of the liver. *AJR* 2007. doi:10.2214/AJR.06.0992
* Boyce CJ et al. Hepatic steatosis in asymptomatic adults identified by unenhanced low-dose CT. *AJR* 2010. doi:10.2214/AJR.09.2590
* Pickhardt PJ et al. Specificity of unenhanced CT for non-invasive diagnosis of hepatic steatosis. *Eur Radiol* 2012. doi:10.1007/s00330-011-2349-2

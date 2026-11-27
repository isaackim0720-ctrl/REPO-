# HUnter: automatic liver (3-circle) and spleen attenuation from CT

*HUnter hunts down the Hounsfield units (HU) of the liver and spleen on CT.*

Upload a CT scan and get the Hounsfield-unit (HU) values a reader would measure with
circular ROIs, three in the liver and one in the spleen:

All three liver circles go on **one axial slice**: the slice (in the middle part of the liver)
where all three fit in the most uniform tissue.

| ROI | Where | Rule |
|---|---|---|
| **LL** | Left lobe | inside Couinaud segments 2/3, **left of the falciform ligament** (≥ 10 mm from it) |
| **RL1** | Right lobe, front (top of the image) | segments 5/8, right of the ligament |
| **RL2** | Right lobe, back (bottom of the image) | segments 6/7, below RL1 in the image, not overlapping it |
| **SPL** | Spleen | on the same slice if the spleen fits there, otherwise the spleen's best slice; ≥ 5 mm from its edge |

(If the three circles cannot fit on one slice, they are placed on separate slices and the case is
flagged. `single_slice: False` in `liverhu/config.py` switches to the older separate-slices method:
RL1 upper right lobe in segment 8, RL2 lower right lobe.)

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

Circle positions (one-slice mode): all liver circles near the liver edge; RL1 at the outer
right edge of the right lobe (front sector, segments 5/8); RL2 near the back (bottom of the
image, segments 6/7), roughly beneath RL1, with at least 10 mm of liver between them. The
spleen circle is the largest circle that fits anywhere in the spleen.

Defaults (change on the website under *More settings*, or in `liverhu/config.py`): liver circle
diameter 30 mm (7.07 cm²; 25, 20, then 15 mm only if it does not fit, noted in the result),
edge gap 5 mm, ligament gap 10 mm. Use the same settings for every scan in
a study; they are saved in every result row.

## Automatic checks

Every scan gets a **quality badge** (✓ Good / ⚠ Check / ✕ Poor quality) and, if needed, an
**Incomplete scan** badge, with the reasons listed. Every circle's **mean HU** gets a **⚠ low** or
**⚠ high** mark when it is outside the expected range. These also appear in the check image and in
Excel (`LL_HU_check` …, `quality`, `quality_notes`, `scan_complete`, `completeness_notes`).

* **Contrast phase** is detected first, because normal values differ: from the aorta's HU
  (≥ 130 HU contrast-enhanced, < 90 HU non-contrast), else the DICOM contrast tag, else the spleen.
* **Expected mean HU** (change in `liverhu/config.py`):

  | | non-contrast | contrast-enhanced |
  |---|---|---|
  | liver circles | 40–80 HU | 60–170 HU |
  | spleen circle | 30–65 HU | 70–180 HU |

  Non-contrast liver: < 40 HU is the moderate–severe fatty-liver cut-off (Boyce et al., AJR 2010);
  > 80 HU is about mean + 2 SD of 3,357 adults in the same study (58.8 ± 10.8 HU). The other
  ranges are rough plausibility ranges. If the phase is uncertain, the widest range is used.
  A "low" liver value is expected in fatty liver: the mark means *look at it*, not *error*.
* **Incomplete scan**: liver cut off at the top/bottom of the scan or at the image side, or
  slices missing from the DICOM series (spleen cut-off is noted too).
* **Quality**: *poor* if the image is very noisy (circle SD > 40 HU), not in Hounsfield units, or
  not all liver circles could be placed; *check* if noisy (SD > 25 HU), slices > 5 mm, unusual liver
  volume, very uneven liver (> 30 % unusual tissue), relaxed placement or no common slice.

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

* On the server computer open <http://localhost:8000>.
* Other devices on the same network open the **https** address the start window prints, e.g.
  `https://192.168.1.23:8443`. Everything is encrypted, including the password. The first time,
  the browser warns that the connection is not private (the certificate is self-made): choose
  **Advanced → Proceed** (Chrome/Edge) or **Show Details → visit this website** (Safari).
  Plain `http://<address>:8000` only works on the server computer itself (start with `--lan-http`
  to allow it from other computers too; not recommended, because it is not encrypted).
* From anywhere else, see *Open HUnter from anywhere* below.
* Log in: **user name = your name** (it is recorded with your uploads), **password = the lab password**.
* Drag in a folder exported from your scan database (DICOM, any folder layout, many patients
  at once), `.zip` files, or `.nii/.nii.gz`. Each study is measured separately, one at a time,
  about 2–4 minutes per scan on a normal PC.
* Open the upload to watch progress, check the QA images, and download Excel/CSV.
  *All results (Excel)* in the top bar gives every scan ever measured.

**Scan library (store now, measure later):** open **Library** in the top bar and drop studies
there. They are only stored, not measured. The list shows every stored study with its series,
number of images, slice thickness and size, plus who added it and when. Studies uploaded twice
are pointed out. To measure, tick studies (click a row, or Shift-click to select a range) and
press **Measure selected**. They join the one measuring queue and are measured one after another,
in the order you ticked them, and you can close the page meanwhile. Each row then shows liver
and spleen HU, with a link to the full result. **Stop waiting** takes studies out of the queue
again, and measuring a study again replaces its result. The circle size and other measuring
settings are chosen when you press Measure. Stored scans stay on the server computer until you
**Delete** them, which removes the scan files and results. The page shows how much space they
use and how much is free, and uploads are refused when less than 2 GB is free.

**Adjusting circles by hand:** on a finished scan, click **Adjust circles**. Pick a circle, click or
drag it to a new spot, change slice or size, and watch mean/SD/min/max update; **Save this circle**
stores it as *moved by hand* (the automatic circle is kept, **Reset to automatic** brings it back).
Liver mean, liver − spleen and liver/spleen update, the check image is redrawn, and the Excel file
shows `LL_manual` etc. This works for scans measured with v0.3 or later, because the liver/spleen
region of each scan is kept for it (about 10–30 MB per scan; untick under *Advanced* to not keep it).

The website is only reachable inside your lab network (or your private Tailscale network, below)
and only with the password. Scans never leave the server computer. Scans uploaded on the
**Measure** page are **deleted right after they are measured**; only the numbers and QA images
are kept (in `liverhu_data/`). Start with `--keep-uploads` to keep them. Scans in the **Library**
stay until you delete them there. Patient names are never read; PatientID/AccessionNumber/StudyDate are added to
results only if you tick that option. Each study also gets a pseudonymous `study_key`.

Change the password: `python -m liverhu set-password` (with the server stopped).

**Built-in protection:**
* Encryption for everyone except the server computer itself.
* Password guessing is blocked: after 10 different wrong passwords from one sender, that sender
  waits 15 minutes.
* Changes sent from other websites are refused (cross-site request forgery).
* Browsers are told not to keep copies of pages or results.
* The password is stored only as a salted hash.

## Run it with no Terminal window (Mac)

```bash
bash scripts/background_mac.sh on
```

HUnter then runs in the background. It starts by itself whenever you log in, keeps the Mac
from dozing off while it runs, and starts again if it ever stops; you can close Terminal.
macOS does not allow background programs in Downloads, Desktop or Documents, so if HUnter is
in one of those, the script offers to move it to your home folder (`~/HUnter`, with results
and password). The Mac must stay on and awake: plugged in, lid open.
`bash scripts/background_mac.sh status` shows the addresses, `... log` the latest messages, and
`... off` stops it. The update script restarts it automatically.

## Open HUnter from anywhere (Tailscale)

[Tailscale](https://tailscale.com) is a free app (Personal plan: up to 6 people, non-commercial
use) that joins your devices, and the people you invite, in a private encrypted network.
HUnter is then reachable from home, school or phone data at an address like
`https://your-mac.tail1234.ts.net`, with a real certificate (no browser warning). It is not on
the public internet: only devices signed in to your Tailscale network can open it, and search
engines cannot find it. The HUnter password is still required. Check with your lab that this
is allowed for the scans you use.

1. On the HUnter Mac, install Tailscale (Mac App Store or <https://tailscale.com/download>),
   open it from the menu bar and log in.
2. In Terminal, in the HUnter folder, run `bash scripts/remote_access_mac.sh`. The first time,
   it may show a link: open it and click **Enable**. It prints the `https://….ts.net` address.
   This stays on after restarts; `bash scripts/remote_access_mac.sh off` turns it off.
3. Invite lab members: <https://login.tailscale.com/admin/users> → **Invite users**. They install
   Tailscale on their own computer or phone, accept, and open the address.

The start message (and `python -m liverhu addresses`) shows the Tailscale address when it is on.

## Updating to a new version

macOS / Linux: in Terminal, inside the installed folder, run `bash scripts/update_mac_linux.sh`,
then restart the website (if it runs in the background, the update restarts it for you).
This keeps the install, the password and all results.
Windows: download the new ZIP, unzip it, and copy its `liverhu` folder over the old one
(keep your `.venv` and `liverhu_data`), then restart `start_windows.bat`.

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

Measured on a 4-core computer, liver + spleen, normal scan (168 slices):

| mode | peak RAM | time |
|---|---|---|
| normal | 4.0 GB (large 588-slice scan: 4.4 GB) | ~3 min |
| low-memory | **2.8 GB** | ~2.5 min |

Low-memory mode uses smaller AI tiles and at most 4 CPU threads. On the test scan the
results matched normal mode within 1 HU (liver mean 129.2 vs 129.0 HU, spleen identical).
`scripts/start_mac_linux.sh` switches it on automatically on computers with 8 GB RAM or
less; elsewhere add `--low-memory`. Use one mode for a whole study; it is recorded in the
`model_tile` column. Close other programs on the computer that runs the website. A computer
with an NVIDIA GPU is used automatically and is much faster.

## Tests

`python -m pytest -q tests`. The tests check falciform-side and upper/lower placement on
synthetic livers, exact HU statistics, vessel/cyst avoidance, DICOM loading and orientation,
series choice, no identifiers in results by default, password protection and upload safety.

## References

* Wasserthal J et al. TotalSegmentator. *Radiology: AI* 2023. doi:10.1148/ryai.230024
* Kodama Y et al. Comparison of CT methods for determining the fat content of the liver. *AJR* 2007. doi:10.2214/AJR.06.0992
* Boyce CJ et al. Hepatic steatosis in asymptomatic adults identified by unenhanced low-dose CT. *AJR* 2010. doi:10.2214/AJR.09.2590
* Pickhardt PJ et al. Specificity of unenhanced CT for non-invasive diagnosis of hepatic steatosis. *Eur Radiol* 2012. doi:10.1007/s00330-011-2349-2

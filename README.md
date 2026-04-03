# Homecam Timeline Player

A Windows desktop player for minute-sliced MP4 files.

## Features

- Load a root folder with many MP4 files (recursive scan).
- Large folder loading runs in background without freezing the UI.
- Loading progress is shown as total count, current processed count, and percent.
- Start playback automatically right after a folder is loaded.
- Remember the last opened folder and open the dialog at that location next time.
- Validate each MP4 before indexing (invalid files are skipped with reason).
- Validation is based on MP4 moov-atom presence.
- View invalid file list by clicking the Invalid Files button in the player.
- Invalid Files opens full error content directly.
- Build one virtual timeline across all segments.
- Drag the timeline bar and the preview frame follows that time immediately.
- Missing intervals are marked as timeline gaps only when the gap is 10 seconds or longer.
- Timeline gap and invalid ranges are blended into the seek bar groove for a natural look.
- Seek bar cannot stay inside unplayable ranges (gap/invalid); it snaps to the next playable point.
- Playback also skips unplayable ranges and continues with the next playable segment.
- Continuous playback across file boundaries.
- Playback speed buttons: 1x, 4x, 8x, 16x.
- Volume control slider (0-100%) with remembered setting.
- Playback engine: libmpv (python-mpv), embedded in the app window.

## Run

```powershell
C:/conda/envs/math/python.exe -m pip install -r requirements.txt
C:/conda/envs/math/python.exe homecam_player.py example_video
```

You can also run without argument and choose folder from UI.

## Notes

- This app does not re-encode or merge files.
- Timeline is based on file order inferred from folder/file naming.
- Initial segment duration is assumed to be around 60 seconds and refined when each file is loaded.
- Invalid Files button opens full error report directly (no extra detail click).
- Put `libmpv-2.dll` inside `mpv/` under project root (or next to script) for Windows.

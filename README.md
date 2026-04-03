# Homecam Timeline Player

A Windows desktop player for minute-sliced MP4 files.

## Features

- Load a root folder with many MP4 files (recursive scan).
- Large folder loading runs in background without freezing the UI.
- Loading progress is shown as total count, current processed count, and percent.
- Start playback automatically right after a folder is loaded.
- Remember the last opened folder and open the dialog at that location next time.
- Validate each MP4 before indexing (invalid files are skipped with reason).
- View invalid file list by clicking the Invalid Files button in the player.
- The invalid dialog opens in list mode first, then switches to full error content when you click "자세히 보기".
- Build one virtual timeline across all segments.
- Drag the timeline bar and the preview frame follows that time immediately.
- Missing intervals are marked as timeline gaps only when the gap is 10 seconds or longer.
- Timeline gap and invalid ranges are blended into the seek bar groove for a natural look.
- Continuous playback across file boundaries.
- Playback speed buttons: 1x, 4x, 8x, 16x.
- Volume control slider (0-100%) with remembered setting.

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
- For strict validation, install FFmpeg and make sure `ffprobe` is available in PATH.
- If `ffprobe` is missing, the app falls back to basic header validation.
- Invalid Files button opens full error report directly (no extra detail click).

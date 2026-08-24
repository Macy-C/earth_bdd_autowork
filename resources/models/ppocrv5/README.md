# PP-OCRv5 local models

This directory stores PaddleOCR PP-OCRv5 inference models used by the desktop OCR locator.

Downloaded models:

- `PP-OCRv5_mobile_det_infer`: text detection model
- `PP-OCRv5_mobile_rec_infer`: text recognition model

Official source:

- https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_mobile_det_infer.tar
- https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_mobile_rec_infer.tar

Runtime note:

- `autowork_core/common/ocr_engine.py` loads these local models through the `paddleocr` package.
- `paddleocr` is required. The legacy OCR fallback has been removed.

Install the verified packages with Python 3.11:

```powershell
python -m pip install "paddleocr==3.6.0"
python -m pip install "opencv-contrib-python==4.6.0.66" "PyYAML==6.0.2"
python -m pip check
```

`paddleocr==3.6.0` pulls in `paddlex==3.6.1`. PaddleX installs OpenCV 4.10 by default, but this project also uses `airtest==1.4.3`, which requires `opencv-contrib-python<=4.6.0.66`. After installing PaddleOCR, restore OpenCV to `4.6.0.66`; PP-OCRv5 initialization and OCR inference have been verified with that version.

Runtime tuning is controlled by `OCR_SETTING` in `config/config.yaml`:

- `MODE`: preset baseline, one of `fast`, `balanced`, or `accurate`. Explicit fields override the preset.
- `WARMUP`: preload PaddleOCR and run one small OCR sample during `before_all`.
- `CACHE_TTL`: reuse OCR results for the same sampled screen image within this many seconds.
- `DET_LIMIT_SIDE_LEN` and `DET_LIMIT_TYPE`: limit detection input size. Lower values are faster but can merge nearby UI text.
- `DET_BOX_THRESH`: filter lower-confidence detection boxes. Higher values are faster but may miss low-contrast text.
- `DET_UNCLIP_RATIO`: expand detected text boxes before recognition. Increase it if text is clipped; decrease it if nearby text is merged.
- `RECOGNITION_BATCH_SIZE`: recognition batch size configured during PaddleOCR initialization.
- `TIMEOUT`, `INTERVAL`, `MATCH_MODE`, `DEBUG_ON_FAIL`: default OCR action behavior when not overridden by an action call or locator.

OCR locators can override per-call detection parameters without reinitializing PaddleOCR. The engine is still a singleton; dynamic values are passed to each `predict()` call.

Recommended locator-level overrides:

- `match_mode`, `index`
- `det_unclip_ratio`, `det_box_thresh`
- `det_limit_side_len` only for a few difficult locators
- `cache_ttl` / `use_cache` only for stable screens

Do not use locators to override initialization-level settings such as `WARMUP`, `MODE`, or `RECOGNITION_BATCH_SIZE`.

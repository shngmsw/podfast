# Skill: setup (podcast-edit)

初回セットアップ手順。ユーザーが /setup と入力したらこの手順を実行する。

## 手順

### 1. Python仮想環境
```bash
cd {project_root}/python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. FFmpeg確認
```bash
ffmpeg -version
```
バージョン6以上であること。なければインストール案内:
- macOS: `brew install ffmpeg`
- Ubuntu: `sudo apt install ffmpeg`

### 3. HuggingFace Token設定
話者分離 (pyannote/speaker-diarization) に必要。

1. https://huggingface.co/settings/tokens でトークン取得（無料）
2. https://huggingface.co/pyannote/speaker-diarization-3.1 でライセンス同意
3. https://huggingface.co/pyannote/segmentation-3.0 でライセンス同意

```bash
cp .env.example .env
# .env に HF_TOKEN=hf_xxxxx を記入
```

### 4. GPU確認（任意）
```bash
python -c "import torch; print(torch.cuda.is_available())"
```
- True → GPU使用（高速）
- False → CPU使用（動作するが低速。30分の音声で10-15分程度）

### 5. 動作テスト
```bash
source .venv/bin/activate
python -c "
import whisperx
import torch
from silero_vad import load_silero_vad
print('whisperx:', whisperx.__version__)
print('silero_vad: OK')
print('torch:', torch.__version__)
print('CUDA:', torch.cuda.is_available())
print('セットアップ完了')
"
```

エラーが出たら内容を確認して対処する。
よくあるエラー:
- `ModuleNotFoundError` → requirements.txtの再インストール
- CUDA関連 → CPU fallbackで動作するので無視可
- pyannote認証エラー → HF_TOKENとライセンス同意を確認

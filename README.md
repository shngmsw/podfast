# podfast

録音ファイルを渡すだけ。無音カット、フィラーカット、言い直し検出、話者分離、文字起こし、チャプター生成、ショーノーツ作成、音声書き出し。全部自動。

[Claude Code](https://docs.anthropic.com/en/docs/claude-code) のスキルとして動作するポッドキャスト自動編集パイプラインです。

## Features

- OBSマルチトラック / ステレオ分離 / シングルトラック入力に対応
- WhisperX による高精度な日本語文字起こし（word-level timestamps）
- Silero VAD による無音区間検出
- クロストーク（発話被り）検出 + ダッキング / ミュート処理
- フィラー検出（29パターン辞書）
- Claude Code による言い直し検出・品質チェック・チャプター生成・ショーノーツ作成
- LUFS -16 ラウドネス正規化（2パス）
- 48kHz / 320kbps 高音質出力

## Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- Python 3.11+
- FFmpeg 6+
- CUDA 対応 GPU（推奨。CPU でも動作するが低速）
- HuggingFace Token（シングルトラック時の話者分離用。マルチトラック時は不要）

## Quick Start

```bash
# 1. セットアップ
/setup

# 2. OBS録音を編集（音声トラック自動検出）
/generate ~/obs_recording.mp4 --speakers "ホスト,ゲスト"

# 3. マルチトラック入力
/generate --tracks "~/host.wav,~/guest.wav" --speakers "ホスト,ゲスト"

# 4. シングルトラック
/generate ~/podcast.wav --num-speakers 2
```

## Pipeline (13 Steps)

| Step | 処理 | 実行 |
|------|------|------|
| 1. preprocess | 入力モード判定 + トラック分離 + 16kHz/48kHz変換 | Python |
| 2. stt | WhisperX 文字起こし（マルチ時はトラック別） | Python |
| 3. vad | Silero VAD 無音区間検出 | Python |
| 4. filler_detect | フィラー検出（29パターン辞書） | Python |
| 4b. crosstalk | クロストーク検出 + ダッキング/ミュート提案 | Python |
| 4c. crosstalk_review | クロストーク確認・修正 | Claude Code |
| 5. filler_review | フィラー検出結果の確認・修正 | Claude Code |
| 6. retake_detect | 言い直し検出 | Claude Code |
| 7. review | 誤字脱字修正 + 固有名詞チェック | Claude Code |
| 8. cut_proposal | 全結果統合 → カット提案 JSON | Python |
| 9. audio_edit | カット + クロストーク処理 + ミックスダウン + LUFS正規化 | Python |
| 10. transcript | 話者ラベル付き Markdown トランスクリプト | Python |
| 11. chapters | チャプター区切り + タイトル生成 | Claude Code |
| 12. shownotes | ショーノーツ（要約・ハイライト・リンク） | Claude Code |

## Output

```
runs/{run_id}/
├── output.mp3           # 編集済み音声
├── transcript.md        # 話者ラベル付き全文トランスクリプト
├── chapters.json        # チャプターマーカー
├── chapters.txt         # Podcastアプリ用チャプターテキスト
├── shownotes.md         # ショーノーツ
└── edit_report.md       # 編集レポート
```

## Input Modes

| モード | 入力 | 話者識別 | HF_TOKEN |
|--------|------|----------|----------|
| マルチトラック | 話者ごとの別ファイル | トラック=話者 | 不要 |
| OBSマルチトラック | MP4/MKV（複数audio stream） | ストリーム=話者 | 不要 |
| ステレオ分離 | L/R分離ステレオファイル | チャンネル=話者 | 不要 |
| シングルトラック | モノラル/ミックス | WhisperX diarization | 必要 |

## License

MIT License. See [LICENSE](LICENSE).

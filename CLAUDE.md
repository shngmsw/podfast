# podcast-edit: ポッドキャスト自動編集スキル

録音ファイルを渡すだけ。無音カット、フィラーカット、言い直し検出、
話者分離、文字起こし、チャプター生成、ショーノーツ作成、音声書き出し。全部自動。

## クイックスタート
/setup                              # 初回セットアップ
/generate ~/Desktop/podcast.wav     # 編集実行（モノラル/ステレオ自動判定）

### マルチトラック入力（推奨）
話者ごとに別ファイルで録音している場合:
/generate --tracks "~/track_host.wav,~/track_guest.wav" --speakers "ホスト,ゲスト"

OBSで録音したMP4（複数音声トラック入り）の場合:
/generate ~/obs_recording.mp4 --speakers "ホスト,ゲスト"
→ 音声トラック数を自動検出し、トラック別に分離。映像は破棄。

ステレオ1ファイルで左=話者A, 右=話者B の場合:
/generate ~/podcast_stereo.wav --stereo-split --speakers "ホスト,ゲスト"

### シングルトラック入力
全員が1トラックにミックスされている場合（WhisperXの話者分離を使用）:
/generate ~/podcast_mono.wav --num-speakers 2

## 環境要件
- Python 3.11+
- FFmpeg 6+
- CUDA対応GPU推奨（CPUでも動作可、低速）
- HuggingFace Token（シングルトラック時の話者分離用、無料）
  ※ マルチトラック入力時はHF_TOKEN不要

## 入力モード
| モード | 入力 | 話者識別 | HF_TOKEN |
|--------|------|----------|----------|
| マルチトラック | 話者ごとの別ファイル | トラック=話者（確実） | 不要 |
| OBSマルチトラック | MP4/MKV（複数audio stream） | ストリーム=話者（確実） | 不要 |
| ステレオ分離 | L/R分離ステレオファイル | チャンネル=話者（確実） | 不要 |
| シングルトラック | モノラル/ミックス | WhisperX diarization | 必要 |

OBSマルチトラック自動検出: MP4/MKV入力時にffprobeで音声ストリーム数を確認。
2本以上の音声ストリームがあれば自動的にobs_multitrackモードで処理する。
OBSのトラック設定でtitleタグが付いていれば話者名として使用。なければ--speakersで指定。

## パイプライン (13ステップ)
1. preprocess      - 入力モード判定 + トラック分離 + 16kHz変換
2. stt             - トラック別WhisperX文字起こし（マルチ時は話者分離不要）
3. vad             - トラック別Silero VAD 無音区間検出
4. crosstalk       - クロストーク検出 + メイン話者判定 + ダッキング/ミュート提案
5. filler_detect   - フィラー検出 (29パターン辞書)
6. filler_review   - Claude Codeがフィラー検出結果を確認・修正
7. retake_detect   - Claude Codeが言い直しを検出
8. review          - Claude Codeが誤字脱字修正 + 固有名詞チェック
9. cut_proposal    - 全結果統合 → カット提案JSON生成
10. audio_edit     - トラック別カット + クロストーク処理 + ミックスダウン + LUFS正規化
11. transcript     - 話者ラベル付きMarkdownトランスクリプト生成
12. chapters       - Claude Codeがチャプター区切り + タイトル生成
13. shownotes      - Claude Codeがショーノーツ (要約・ハイライト・リンク) 生成

## 出力ファイル
runs/{run_id}/
├── output.mp3                # 編集済み音声 (LUFS -16正規化)
├── tracks/                   # トラック別の中間ファイル（デバッグ用）
├── transcript.md             # 話者ラベル付き全文トランスクリプト
├── chapters.json             # チャプターマーカー (タイムスタンプ + タイトル)
├── chapters.txt              # Podcastアプリ用チャプターテキスト
├── shownotes.md              # ショーノーツ (要約・ハイライト)
└── edit_report.md            # 編集レポート (カット数・削減率・クロストーク処理数等)

## クロストーク処理
複数トラックで発話が被った区間の処理方式:

| 状況 | 処理 |
|------|------|
| 片方がメイン発話 + 相手が相槌/笑い | 相槌側をダッキング (-12dB) |
| 片方がメイン発話 + 相手が割り込み（短い） | 割り込み側をミュート |
| 双方が同程度に発話（議論） | そのまま残す |
| 片方が言い直しのきっかけになった被り | 言い直し前をカット（retake_detectと連携） |

判定ロジック:
- 各トラックのVAD結果を重ね合わせ、重なり区間を検出
- 重なり区間内のSTTテキスト量（文字数）でメイン話者を判定
- 短い相槌（1秒未満 + テキスト3文字以内）は自動でダッキング
- それ以外はClaude Codeが文脈を見て判断

## 話者設定
- デフォルト: 自動検出 (WhisperXのdiarization)
- カスタム: /generate --speakers "田中,佐藤" で話者名を指定可能
- 話者数ヒント: /generate --num-speakers 3 で話者数を明示

## 音声品質設定
- ラウドネス: LUFS -16 (Podcast標準)
- サンプルレート: 44100Hz
- ビットレート: 192kbps (MP3) / 128kbps (M4A)
- 形式: --format mp3 (デフォルト) または --format m4a

# Skill: generate (podcast-edit)

/generate {音声ファイルパス} [--speakers "名前1,名前2"] [--num-speakers N] [--format mp3|m4a]
/generate --tracks "track1.wav,track2.wav" --speakers "ホスト,ゲスト" [--format mp3|m4a]
/generate {ステレオファイル} --stereo-split --speakers "ホスト,ゲスト"

## 事前準備
- run_id を現在時刻から生成: YYYYMMDD_HHMMSS
- 作業ディレクトリ: {project_root}/runs/{run_id}/
- 各ステップの出力は runs/{run_id}/step{NN}_{name}/ に保存

## Step 1: preprocess (Python)
```bash
# マルチトラック（別ファイル）
.venv/bin/python step01_preprocess.py \
  --tracks "{track1},{track2}" --speakers "{name1},{name2}" \
  --output ../runs/{run_id}/step01_preprocess

# OBS録音MP4（音声トラック自動検出・分離）
.venv/bin/python step01_preprocess.py \
  --input {recording.mp4} --speakers "{name1},{name2}" \
  --output ../runs/{run_id}/step01_preprocess

# ステレオ分離
.venv/bin/python step01_preprocess.py \
  --input {ファイル} --stereo-split --speakers "{name1},{name2}" \
  --output ../runs/{run_id}/step01_preprocess

# シングルトラック
.venv/bin/python step01_preprocess.py \
  --input {ファイル} --output ../runs/{run_id}/step01_preprocess
```
処理内容:
- 入力モード判定（multitrack / obs_multitrack / stereo_split / single）
- MP4/MKV入力時: ffprobeで音声ストリーム数を確認、2本以上なら自動分離
- 各トラックを16kHz mono WAVに変換（映像は破棄）
- マルチトラック時はミックスダウンも生成
- OBSのストリームtitleタグがあれば話者名に使用
- 出力: metadata.json, tracks/track_XX_{name}_16k.wav

metadata.jsonのmodeを以降のステップで参照し、処理を分岐する。

## Step 2: stt (Python)
```bash
.venv/bin/python step02_stt.py \
  --audio {トラック別WAVまたはmixed} \
  --output ../runs/{run_id}/step02_stt \
  [--num-speakers N]
```
処理内容:
- **マルチトラック時**: 各トラックを個別にWhisperXで文字起こし。話者分離不要（トラック=話者）。
  各トラックのstt結果を統合し、タイムスタンプ順にソートして1つのstt_result.jsonにまとめる。
- **シングルトラック時**: ミックス音声をWhisperX + pyannote話者分離で処理。
- 出力: stt_result.json

## Step 3: vad (Python)
```bash
.venv/bin/python step03_vad.py \
  --audio {トラック別WAVまたはmixed} \
  --stt ../runs/{run_id}/step02_stt/stt_result.json \
  --output ../runs/{run_id}/step03_vad
```
処理内容:
- **マルチトラック時**: 各トラックで個別にSilero VAD実行。トラックごとの結果を保存。
  → vad_{speaker}.json を話者分出力
- **シングルトラック時**: ミックス音声で1回VAD実行。
- ポッドキャスト向けパラメータ: min_silence_duration_ms=500, speech_pad_ms=200

## Step 4: filler_detect (Python)
```bash
.venv/bin/python step04_filler_detect.py \
  --stt ../runs/{run_id}/step02_stt/stt_result.json \
  --vad ../runs/{run_id}/step03_vad/vad_result.json \
  --output ../runs/{run_id}/step04_filler_detect
```
29パターン辞書でフィラーを検出。文頭/文中を区別。

## Step 4b: crosstalk (Python) ★マルチトラック時のみ
```bash
.venv/bin/python step04b_crosstalk.py \
  --vad-dir ../runs/{run_id}/step03_vad \
  --stt ../runs/{run_id}/step02_stt/stt_result.json \
  --metadata ../runs/{run_id}/step01_preprocess/metadata.json \
  --output ../runs/{run_id}/step04b_crosstalk
```
処理内容:
- 複数トラックのVAD結果を重ね合わせて発話重なり区間を検出
- 自動分類: mute（短い割り込み）/ ducking（相槌-12dB）/ keep / review
- action=review の区間はStep 4c でClaude Codeが判断
- シングルトラック時は自動スキップ
- 出力: crosstalk_result.json

## Step 4c: crosstalk_review (Claude Code) ★マルチトラック時のみ
crosstalk_result.json の action=review 区間を確認。

判断基準:
- 両者の発話内容を読み、文脈的にどちらがメインかを判定
- メイン話者でない方を ducking (-9dB) に設定
- 議論として双方の発話が重要な場合は keep
- 片方が明らかに話を遮っている場合は mute
- 迷ったら ducking（ミュートより安全）

crosstalk_result.json を更新して保存。

## Step 5: filler_review (Claude Code)
STTテキストを通し読みしてStep 4の検出結果を確認・修正。

## Step 6: retake_detect (Claude Code)
.claude/skills/retake-detect/SKILL.md の判断基準に従い言い直しを検出。

注意: マルチトラック時は被り（クロストーク）が言い直しのきっかけになることがある。
crosstalk_result.jsonも参照し、被り直後の言い直しを優先的にチェック。

## Step 7: review (Claude Code)
.claude/skills/review/SKILL.md の判断基準に従い品質チェック。

## Step 8: cut_proposal (Python)
```bash
.venv/bin/python step08_cut_proposal.py \
  --vad ../runs/{run_id}/step03_vad/vad_result.json \
  --filler ../runs/{run_id}/step04_filler_detect/filler_result.json \
  --retake ../runs/{run_id}/step06_retake/retake_result.json \
  --metadata ../runs/{run_id}/step01_preprocess/metadata.json \
  --output ../runs/{run_id}/step08_cut_proposal
```
全結果を統合してカット提案を生成。クロスフェード50msマーカー付与。

## Step 9: audio_edit (Python)
```bash
.venv/bin/python step09_audio_edit.py \
  --metadata ../runs/{run_id}/step01_preprocess/metadata.json \
  --cuts ../runs/{run_id}/step08_cut_proposal/cut_proposal.json \
  --crosstalk ../runs/{run_id}/step04b_crosstalk/crosstalk_result.json \
  --output ../runs/{run_id}/step09_audio_edit \
  --format {mp3|m4a} --lufs -16
```
処理内容:
- **マルチトラック時**:
  1. 各トラックに個別にカット適用
  2. crosstalk_resultに基づきダッキング/ミュート適用（volume filter）
  3. 全トラックをamixでミックスダウン
  4. LUFS -16正規化 + エンコード
- **シングルトラック時**: 従来どおり1トラックでカット→正規化→エンコード
- 出力: output.mp3 (or .m4a), tracks/ (中間ファイル)

## Step 10: transcript (Python)
話者ラベル付きMarkdownトランスクリプト生成。2分ごとにセクション区切り。

## Step 11: chapters (Claude Code)
transcript.md を読みチャプター生成。1チャプター2-10分、タイトル15字以内。

## Step 12: shownotes (Claude Code)
タイトル案3つ、3行要約、チャプター一覧、ハイライト3-5個、キーワード、関連リンク。

## 完了報告
- 元音声: {duration}秒 → 編集後: {duration}秒（削減率 {rate}%）
- カット数: 無音{n}件, フィラー{n}件, 言い直し{n}件
- クロストーク処理: ミュート{n}件, ダッキング{n}件
- 入力モード: {multitrack|stereo_split|single}
- 話者数: {n}人
- チャプター数: {n}
- 出力ファイル一覧とパス

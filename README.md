# おんどく ボイス（研究用プロトタイプ）

児童生徒が自分の声を録音すると、その声に似せたAI音声で英語・日本語の課題文を読み上げ、本人が聞いて音読練習に使えるWebアプリです。

- 参加者はブラウザだけで使えます（Windows / Mac / Chromebook / iPad。インストール不要）。
- 音声の生成は**研究者が管理するMac（またはPC）の中だけ**で行い、外部のAIサービスには音声も文章も送りません。
- 生成には、オープンソースの **XTTS-v2**（`pip install` だけで導入できる音声合成モデル）を使います。

```
参加者のブラウザ                       研究者のMac（このアプリ）
 録音 / ファイル選択  ──HTTPS──▶  Webサーバ ─▶ 生成キュー ─▶ XTTS-v2
 生成音声の再生      ◀──────────  保存（data/ フォルダ）◀──┘
                                    管理画面 /admin
```

## 機能

| 参加者（`/`） | 研究者（`/admin`） |
| --- | --- |
| ID と あいことば でログイン | 参加者ID・あいことばの一括発行（CSV保存） |
| 画面の文章を読んで声を登録（録音 or 音声ファイル） | 課題文の登録（1行1文で一括、言語は自動判定、グループ別配信） |
| 録音の長さ・音量をその場でチェック | 生成状況の確認、試聴、再生成、失敗分の一括再生成 |
| 課題文ごとにAI音声を再生（ゆっくり / ふつう） | 公開前の承認（任意） |
| 自分の音読を録音してAI音声と聞き比べ（録音はブラウザ内だけで、送信しない） | 利用ログのCSV出力（ログイン・再生・練習） |
| | 参加者単位のデータ完全削除（録音・生成音声・ログ） |

生成音声はアプリ内でのみ再生でき、ダウンロードボタンは表示しません。

## 1. 準備するもの

- 研究者用の Mac（Apple Silicon 推奨、メモリ 16GB 以上、空き容量 10GB 以上）
- Python 3.10〜3.13（[python.org](https://www.python.org/downloads/macos/) のインストーラで可）
- 参加者が自宅から使う場合は、HTTPS で公開する手段（後述）

## 2. まずAIなしで動かしてみる（5分）

ターミナルで次を実行します。

```bash
git clone <このリポジトリのURL> ondoku-voice
cd ondoku-voice
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
ONDOKU_ENGINE=dummy python -m app
```

- 管理画面: http://127.0.0.1:8000/admin （パスワードは起動時の表示、または `data/admin-password.txt`）
- 参加者画面: http://127.0.0.1:8000/

`dummy` エンジンは声の代わりに「ピポパ」という音を作るだけですが、録音 → 生成 → 再生の流れをすべて確認できます。

## 3. 音声生成AI（XTTS-v2）を入れる

```bash
source .venv/bin/activate
pip install -r requirements-xtts.txt
```

1. XTTS-v2 のモデルは **Coqui Public Model License（非商用）** です。[ライセンス](https://coqui.ai/cpml)を確認し、研究利用で問題なければ `.env` の `COQUI_TOS_AGREED=1` にします。
2. `.env` の `ONDOKU_ENGINE=xtts` を確認します。
3. 動作確認（初回はモデル約1.8GBをダウンロードします）:

```bash
# 6秒以上の自分の声の WAV を用意して
python scripts/try_engine.py my_voice.wav "きょうは いい てんき です。"
python scripts/try_engine.py my_voice.wav "I like reading books." --lang en
# → try_engine_out.wav ができれば成功。表示される秒数が1文あたりの生成時間の目安です。
```

4. アプリを起動: `python -m app`

GPT-SoVITS のような専用WebUIや個別の学習は不要で、声の登録（数秒〜数十秒）だけで生成できます。Mac では CPU で動かします（XTTS-v2 は Apple の MPS では不安定なため）。

**生成時間の見積もり**: `try_engine.py` で測った1文あたりの秒数 × 参加者数 × 課題文数 が、全員分を作り終えるまでの時間です（例：30名 × 10文 = 300件）。生成は順番待ちで自動的に進み、参加者は作り終わったものから聞けます。声の登録を実験の数日前に済ませておくと、当日待たせずに済みます。

### 別のエンジン（実験的）

`ONDOKU_ENGINE=chatterbox` で Resemble AI の Chatterbox Multilingual（MITライセンス、日英対応）に切り替えられます。torch のバージョンを固定するため、別の仮想環境に `pip install chatterbox-tts` してください。XTTS-v2 の日本語の質が合わない場合の比較用です。

## 4. 参加者に使ってもらう

ブラウザのマイク機能は **HTTPS（または同じMac上の localhost）でしか使えません**。参加者の端末から使う方法は次のどちらかです。どちらを使うかは、所属機関の情報セキュリティ規程と倫理審査の記載に合わせてください。

**A. 自宅など学外から使う（トンネル）**

Mac から外向きに接続して HTTPS のURLを発行するサービスを使います。ルーターの設定は不要です。

```bash
# Cloudflare Tunnel の例（brew install cloudflared）
cloudflared tunnel --url http://127.0.0.1:8000
# Tailscale Funnel の例
tailscale funnel 8000
```

- 表示された `https://...` のURLを参加者に配ります。`.env` は `ONDOKU_SECURE_COOKIE=1` にします。
- 通信はトンネル事業者のサーバを経由します（AIサービスではありませんが、倫理審査の説明に含めてください）。お試し用の一時URL（trycloudflare.com）は起動のたびに変わるため、本番では名前付きトンネルを設定してください。

**B. 学校の中だけで使う（同じWi-Fi）**

1. [mkcert](https://github.com/FiloSottile/mkcert) で証明書を作り、`.env` の `ONDOKU_SSL_CERTFILE` / `ONDOKU_SSL_KEYFILE` に指定
2. `ONDOKU_HOST=0.0.0.0`、`ONDOKU_SECURE_COOKIE=1`
3. 参加者端末に mkcert のルート証明書を入れるか、マイクの代わりに「ファイルを選ぶ」で音声ファイルを送ってもらいます

## 5. 研究者の操作の流れ

1. `/admin` →「参加者」で人数（例: 30）を入れて発行 →「CSVを保存」。**あいことばは発行時にしか表示されません。** 氏名との対応表はアプリの外で管理します。
2. 「課題文」で文章を登録（1行に1文。言語は自動判定）。グループを指定すると、そのグループの参加者だけに配信されます。
3. 「設定・ログ」で声の登録で読ませる文章を確認（6〜30秒で読める長さ）。必要なら「承認してから公開」をオン。
4. 参加者がログインして声を登録すると、自動で全課題文の生成が始まります。課題文をあとから追加・変更した場合も自動で生成されます。
5. 「生成音声」で試聴し、不自然なものは「再生成」。
6. 研究終了時や同意撤回時は「参加者」→「データ削除」。

## 6. データと運用上の注意

- 保存場所はすべて `data/`（`ONDOKU_DATA_DIR`）の中です: `ondoku.sqlite3`（参加者ID・課題文・ログ）、`references/`（登録した声）、`clips/`（生成音声）。氏名は保存しません。
- `data/` は Git に含まれません（`.gitignore`）。バックアップは暗号化した外付けディスクなど研究者の管理下に限り、iCloud などクラウド同期フォルダには置かないでください。
- Mac の FileVault（ディスク暗号化）をオンにしてください。
- 実験期間中は Mac がスリープしないようにします: `caffeinate -dims python -m app`
- 管理者パスワードは `.env` の `ONDOKU_ADMIN_PASSWORD` で固定できます。自動生成した場合は、控えたあと `data/admin-password.txt` を削除してください。
- 同じIPからログイン失敗が続くと5分間ロックされます。

## 7. 未確認の点（プロトタイプとして）

- XTTS-v2 の組み込みは公開APIに沿って書いていますが、**この開発環境ではモデルをダウンロードできず、実際の生成は未確認です**。最初に `scripts/try_engine.py` で動作と品質を確認してください。
- 子どもの声でのクローン品質、日本語の声で英語を話させたときの自然さは、モデルの学習データ（主に大人の声）に依存します。パイロットで確認が必要です。
- 動作確認は Chromium（PC・スマホ幅）で行いました。Safari / iPad / Chromebook では実験前に確認してください。

## 開発者向け

```bash
pip install -r requirements-dev.txt
python -m pytest
```

構成: `app/main.py`（API）、`app/store.py`（SQLite とデータ規則）、`app/worker.py`（生成キュー）、`app/engines/`（音声エンジン）、`app/static/`（画面。ビルド不要の HTML/JS）。録音はブラウザ側で 24kHz モノラル WAV に変換して送るため、サーバに ffmpeg は不要です。

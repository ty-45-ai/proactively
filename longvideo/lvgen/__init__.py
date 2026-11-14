"""lvgen: 画像1枚から長尺動画を作るための、LongCat-Video 用の自前ジョブ実行系。

構成:
- videoio.py  : FFmpeg を使った動画の読み書き（GPU不要）
- jobs.py     : ジョブ（1本の動画）とセグメント（約5秒の断片）の保存・状態管理
- backends.py : 実際の生成処理（LongCat-Video / テスト用モック）
- runner.py   : ジョブを先頭から順に生成し、途中から再開できるようにする本体
- worker.py   : 常駐して、キューに入ったジョブを処理するプロセス
- cli.py      : コマンドラインから1本生成する（動作確認用）
"""

__version__ = "0.1.0"

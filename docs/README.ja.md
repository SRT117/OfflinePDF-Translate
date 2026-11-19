<p align="center">
  <img src="../assets/icon.png" width="96" height="96" alt="OfflinePDF Translate">
</p>

<h1 align="center">OfflinePDF-Translate</h1>

<p align="center">
  完全ローカル・オフライン学術PDF翻訳ワークベンチ · 対訳表示 · 訳文出力 · ハードウェア適応型推論
</p>

<p align="center">
  <a href="#インストールと使い方">インストールと使い方</a>
  · <a href="#データとプライバシー">データとプライバシー</a>
  · <a href="#システム互換性">システム互換性</a>
  · <a href="#よくある質問">よくある質問</a>
  · <a href="BUILD.md">ビルド手順</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Windows-10%2F11%20x64-0078D6?style=flat-square" alt="Windows 10/11 x64">
  <img src="https://img.shields.io/badge/Python-3.13%20Bundled-3776AB?style=flat-square" alt="Python 3.13">
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg?style=flat-square" alt="AGPL-3.0 License"></a>
</p>

<p align="center">
  <a href="../README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | 日本語 | <a href="README.ko.md">한국어</a> | <a href="README.ru.md">Русский</a>
</p>

---

OfflinePDF Translateは、ローカル・オフラインでのPDF文献翻訳に特化したプロジェクトです。インストーラーをダウンロードすれば、ネットワーク接続が一切ないオフラインPCでもそのままインストールして起動でき、PDF文献や日常的な翻訳作業を無料かつ無制限に行えます。インストーラーをUSBメモリに保存しておけば、別のオフラインPCに挿してすぐにインストール・実行することも可能です。Tencent Hunyuan MT2-1.8Bモデルをベースに、38種類の言語および方言の相互翻訳に対応しています。

本プロジェクトはBabelDOCとDocLayout-YOLOによるPDF解析・レイアウト認識・対訳組版（テキスト抽出可能なPDFのみ対応、スキャン画像PDFには非対応）と、llama.cppおよびTencent Hunyuan（Hy-MT2-1.8B）によるローカル推論翻訳を組み合わせています。独立したPythonランタイム、推論エンジン、量子化モデル、フォント・レイアウト資産を内蔵し、バッチキュー、対訳PDFおよび訳文のみPDFの出力、ドキュメント単位のプロセス分離、自動ハードウェアスケジューリング（CPUのみでも安定動作）に対応しています。

## インストールと使い方

### Windowsワンクリックインストール

1. GitHub Releasesから最新版の `OfflinePDF-Translate-Setup.exe` をダウンロードします。
2. ダブルクリックしてインストールします。個人開発のオープンソースプロジェクトであり商用コード署名証明書を購入していないため、Windows SmartScreenの警告が表示された場合は**「詳細情報」→「実行」**をクリックして続行してください。
3. アプリを起動し、PDFファイルをドラッグ＆ドロップして下部の翻訳先言語を選択し、開始をクリックします。

### 出力と品質の境界

- 元のディレクトリーに `[元の名前].翻译.pdf` と `[元の名前].对照.pdf` が生成され、両方のPDFが有効と検証された場合のみ成功とマークされます。元のディレクトリーに書き込めない場合は、ユーザーデータディレクトリー内の `Translated PDFs` に自動保存されます。
- 再翻訳が成功すると、同ディレクトリー内の一時ファイル経由で同名ファイルがアトミックに置換されます。失敗した場合は元の出力が保持されます。
- プレースホルダーの欠落、出力の途切れ、空訳、または原文のそのままの返却が疑われる場合は自動的にリトライします。リトライ後も問題が残る場合、レイアウト維持のためプレースホルダー修復や原文保持を行うため、「完了」ステータスであってもすべての段落が完璧であるとは限りません。重要な文献はリアルタイム表示と訳文をあわせてご確認ください。
- **テキスト抽出可能なPDFのみ対応**：ビットマップのスキャン画像にはOCRエンジンが内蔵されていないため、明確にブロックされ、先にOCRを行うよう案内が表示されます。
- **複雑な文字の組版**：UIには同梱フォントを使用し、PDF組版にはオフラインフォントを埋め込みます。アラビア語などの右から左へ書く文字や一部の複雑な結合文字は、現時点で組版が不正確になる場合があり、該当する翻訳先言語を選択するとUIに警告が表示されます。

### 画面表示言語 / Interface language

ウィンドウ右上で**简体中文、English、日本語、한국어、Русский**を切り替えられ、即座に反映・自動保存されます。ウィンドウ下部の「翻訳先言語」はPDFの訳文言語を独立して制御します。

### 日常翻訳ワークベンチ

上部バーの**日常翻訳**タブに切り替えると、PDF翻訳と競合しないローカル即時テキスト翻訳を利用できます（PDF処理中は自動で排他制御されます）：

- **自動相互翻訳と38言語**：`中 ⇄ 英` の自動判定相互翻訳および対応38言語・方言のストリーミング翻訳（`Ctrl+Enter` または自動翻訳）に対応。
- **PDF改行整理とファイルドロップ**：PDFからコピーした文章の不要な改行やハイフン分断をワンクリックで整形（コードブロック・箇条書きは保持）し、`.txt`、`.md`、`.srt`、`.vtt`、`.csv`、`.tsv`、`.log` のドラッグ＆ドロップにも対応（1回最大5000文字）。
- **Hy-MT2スタイルと用語集**：スタイルプリセット（標準・学術・フォーマル・口語・簡潔）、ヒット語彙のみの用語集注入、選択テキストからの用語登録、ローカル履歴を搭載。

### コマンドライン（CLI）

ソースまたは同梱ランタイム環境からCLIで実行できます：

```powershell
.\runtime\python\python.exe cli.py translate paper.pdf --lang-in auto --lang-out ja
```

`-o/--output`、`-p/--pages`（例：`1-3`、`1,3,5`）、`--threads`、`--parallel`、`--ctx-size`、`--gpu-layers` / `--ngl`、`--model`、`--runtime`、`--no-background` を指定できます。

---

## データとプライバシー

- **完全ローカル・オフライン実行**：アカウント登録不要、テレメトリなし、広告なし、クラウド翻訳APIキーも一切不要です。
- **ループバックネットワーク制限**：起動前にフォント、CMap、トークナイザー、ONNX、GGUFの準備状態をチェックします。翻訳サブプロセス内のPythonネットワーク接続はループバックアドレスに制限され、ローカルの `llama-server` は `127.0.0.1` のみで待ち受けます（注：これはアプリ層の方針制限であり、OSレベルのサンドボックス隔離ではありません）。
- **データ保存場所**：キャッシュ、タスクキュー、実行ログはユーザーデータディレクトリー（Windowsでは `%LOCALAPPDATA%\OfflinePDF Translate`）に保存され、インストールディレクトリーへの書き込み権限は不要です。旧バージョンのデータディレクトリーも引き継がれ、アンインストール時に実行中のキャッシュやログは自動削除されません。ドキュメントの中間ファイルは独立した一時ディレクトリーに置かれ、終了時にクリーンアップされます。

---

## システム互換性

- **Windows 10/11（64ビット、x64）**：公式に完全サポート。すべてのランタイムを内蔵した単一ファイルインストーラーを提供し、インストール時に最低OSバージョンを検証します。
- **Windows 7/8**：**非対応**（同梱のPython 3.13およびPySide6 6.11は旧版Windowsのサポートを終了しています）。
- **macOS / Linux**：公式の署名済みインストーラーは提供していません。コアコードには基本的なプラットフォーム適応とソーステスト互換性が残されていますが、Python 3.13環境と各プラットフォーム向けの `llama-server` をご自身で用意する必要があります。

---

## スケジューリングとハードウェア方針

- ドキュメントはキュー順に処理され、単一ドキュメント内では利用可能メモリとデバイスから算出した推論スロットで並列実行されます。各ドキュメントは独立した分離プロセスで動作します。
- デフォルトの単一段落推論タイムアウトは600秒（10分）、無進捗タイムアウトは900秒で、省電力ノートPCでのCPUのみによる夜間連続稼働やシステムのスリープ防止ウェイクロックに対応しています。WindowsではJob Objectにより組版補助サブプロセスを一括回収します。
- スレッド数、並列スロット、コンテキスト長、GPUオフロード層数はすべて自動検出とUIからの手動微調整に対応しています。

---

## よくある質問

### 独立GPUがなくCPUのみでも使えますか？
はい。インストーラーには各種CPU命令セット向けバックエンドが同梱されており、お使いのCPUを自動判定して適切なスレッド数とコンテキスト長を算出します。ただし長文文献の翻訳には時間がかかります。

### インストーラー起動時に「WindowsによってPCが保護されました」と表示されたら？
個人開発のオープンソースプロジェクトで商用コード署名証明書を購入していないため表示されるSmartScreenの通常プロンプトです。**「詳細情報」→「実行」**をクリックして通常通りインストールできます。

### 一部のPDFで翻訳不可やスキャン画像と判定されるのはなぜですか？
本プロジェクトはPDFに埋め込まれた抽出可能テキストとレイアウトオブジェクトに依存しています。純粋な画像スキャンにはテキスト層がないため、事前にOCRツールでテキスト付きPDFへ変換してからインポートしてください。

### 翻訳後のファイルはどこに保存されますか？
デフォルトでは元のPDFと同じディレクトリー（`[元の名前].翻译.pdf` と `[元の名前].对照.pdf`）に直接出力されます。元のディレクトリーが書き込み保護されている場合は `%LOCALAPPDATA%\OfflinePDF Translate\Translated PDFs` に自動保存されます。

### 起動失敗や翻訳エラーはどう調査すればよいですか？
`%LOCALAPPDATA%\OfflinePDF Translate\logs` 配下の実行ログと `llama-server.log` をご確認ください。Issueを投稿する際は、個人情報や機密情報を除いたエラーログとシステム構成を添付し、プライバシーに関わる論文原文はアップロードしないでください。

---

## ビルドと開発

ソースリポジトリには同梱Pythonランタイム、`llama.cpp` バイナリ、GGUFモデル、および大部分のフォント/ONNX資産は含まれていません。ローカルでのビルド・パッケージ手順は[ビルド手順（docs/BUILD.md）](BUILD.md)をご覧ください。

6つの直接依存関係（`babeldoc`、`pymupdf`、`psutil`、`bitstring`、`tibs`、`PySide6-Essentials`）はいくつかの推移的依存関係（レイアウト処理用のOpenCVやBabelDOC内部で参照される `openai` SDKなど）を導入しますが、これらはクラウド翻訳やOCRが有効であることを意味しません。

---

## 謝辞と関連プロジェクト

OfflinePDF Translateは以下の優れたオープンソースプロジェクトおよびモデルを基盤とし、着想を得ています：

- [BabelDOC](https://github.com/funstory-ai/BabelDOC)（AGPL-3.0）：コアとなるPDF解析、レイアウト復元、数式保護、対訳組版エンジン。
- [PDFMathTranslate / pdf2zh](https://github.com/PDFMathTranslate/PDFMathTranslate)：学術文献の対訳翻訳ワークフローにおける先駆的プロジェクト。
- [llama.cpp](https://github.com/ggml-org/llama.cpp)（MIT）：軽量・高性能なローカルGGUFモデル推論ランタイム。
- [Tencent-Hunyuan/Hy-MT2](https://github.com/Tencent-Hunyuan/Hy-MT2) / [Hy-MT2-1.8B](https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF)：オンデバイス多言語翻訳能力および公式の二軌指示・用語介入・スタイル制御テンプレート仕様を提供。
- [DocLayout-YOLO](https://github.com/opendatalab/DocLayout-YOLO)（AGPL-3.0）：論文レイアウト検出モデル。
- [KFERMercer/RiipL](https://github.com/KFERMercer/RiipL)：Qt 6とHy-MT2ベースのデスクトップ翻訳ツール。日常翻訳モジュールの用語集管理、スタイルプリセット、クリップボードワークフローの着想元。
- [ziniee/local-translate](https://github.com/ziniee/local-translate)：llama.cpp内蔵のローカルデスクトップ翻訳ツール。コードブロックのそのまま透過、長文スライディングウィンドウ分割、ヒット語彙のみ注入する実装の着想元。
- [CopyTranslator](https://github.com/CopyTranslator/CopyTranslator) & [msc306/AI-Translate](https://github.com/msc306/AI-Translate)：PDFコピー時の改行整理、クリップボード監視、ストリーミング速度表示、自動コピーなど日常操作設計の着想元。
- [PySide6 (Qt for Python)](https://doc.qt.io/qtforpython-6/) & [PyMuPDF](https://github.com/pymupdf/PyMuPDF)：デスクトップUIフレームワークおよび低レベルPDF処理基盤。

## 参加とライセンス

- 不具合報告・機能要望：[GitHub Issues](../../../issues)
- 貢献ガイド：[CONTRIBUTING.md](../CONTRIBUTING.md) · 行動規範：[CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md)
- セキュリティポリシー：[SECURITY.md](../SECURITY.md) · 第三者通知：[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)

本プロジェクトのソースコードは[GNU AGPL-3.0](../LICENSE)で公開されています。サードパーティ製コンポーネント、フォント、モデルの重みはそれぞれ元のライセンスに従います。

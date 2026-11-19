# SPDX-License-Identifier: AGPL-3.0-only
from PySide6.QtCore import QLocale, QSettings

LANGUAGES = [('简体中文', 'zh'), ('English', 'en'), ('日本語', 'ja'), ('한국어', 'ko'), ('Русский', 'ru')]
_language = 'zh'

# Interface strings; the document translation language is configured separately.
_TEXT = {
'OfflinePDF Translate — 本地离线PDF文献翻译': ('OfflinePDF Translate — Offline PDF Translation', 'OfflinePDF Translate — オフラインPDF翻訳', 'OfflinePDF Translate — 오프라인 PDF 번역'),
'+ 添加文献': ('+ Add PDFs', '+ PDFを追加', '+ PDF 추가'), '清空队列': ('Clear queue', 'キューを消去', '목록 지우기'),
'文献任务': ('Documents', '文書', '문서'), '展开翻译流': ('Show live view', '翻訳ビューを表示', '번역 화면 열기'), '收起翻译流': ('Hide live view', '翻訳ビューを閉じる', '번역 화면 닫기'),
'添加待翻译 PDF 文献': ('Add PDFs to translate', '翻訳するPDFを追加', '번역할 PDF 추가'),
'将文件拖放到此处，或点击按钮选取 · 纯本地大模型离线处理': ('Drop files here or select them below · Offline local processing', 'ここにファイルをドロップ、または下のボタンで選択 · オフライン処理', '여기에 파일을 놓거나 아래 버튼으로 선택 · 오프라인 처리'),
'选择本地文件': ('Select files', 'ファイルを選択', '파일 선택'), '源语言:': ('Source:', '原文:', '원본:'), '自动识别': ('Auto detect', '自動検出', '자동 감지'), '目标语言:': ('Target language:', '翻訳先言語:', '번역 언어:'), '自动硬件': ('Auto hardware', 'ハードウェア自動設定', '하드웨어 자동 설정'),
'线程:': ('Threads:', 'スレッド:', '스레드:'), '并发:': ('Parallel:', '並列数:', '병렬:'), '窗口:': ('Context:', 'コンテキスト:', '컨텍스트:'), 'GPU 层数:': ('GPU layers:', 'GPUレイヤー:', 'GPU 레이어:'),
'停止': ('Stop', '停止', '중지'), '开始翻译': ('Start translation', '翻訳開始', '번역 시작'),
'对照译本': ('Bilingual PDF', '対訳PDF', '대조 PDF'), '单语译本': ('Translated PDF', '翻訳PDF', '번역 PDF'), '打开目录': ('Open folder', 'フォルダーを開く', '폴더 열기'),
'待执行': ('Pending', '待機中', '대기 중'), '排队中': ('Queued', 'キュー待ち', '대기열'), '完成': ('Done', '完了', '완료'), '已停止': ('Stopped', '停止済み', '중지됨'), '需复核': ('Review needed', '要確認', '검토 필요'), '处理失败': ('Failed', '処理失敗', '처리 실패'),
'实时翻译流': ('Live translation', 'リアルタイム翻訳', '실시간 번역'), '等待任务启动': ('Waiting to start', '開始待ち', '시작 대기 중'), '↑ 回到最新': ('↑ Latest', '↑ 最新へ', '↑ 최신으로', '↑ К последним'), '● 返回实时流': ('● Back to live', '● ライブに戻る', '● 실시간으로'),
'已暂停自动置顶，点击立即回到最新实时翻译段落': ('Auto-scroll paused. Click to return to the latest text.', '自動スクロールを一時停止中。クリックして最新へ戻ります。', '자동 스크롤이 일시 중지되었습니다. 클릭하여 최신 내용으로 이동합니다.'),
'后台正在翻译另一篇文献，点击切回正在进行的实时翻译流': ('Another document is being translated. Click to return to it.', '別の文書を翻訳中です。クリックして戻ります。', '다른 문서를 번역 중입니다. 클릭하여 돌아갑니다.'),
'实时翻译流待启动': ('Live translation is ready', 'リアルタイム翻訳の開始待ち', '실시간 번역 대기 중'), '启动文献翻译后，模型解析出的每一个段落及其即时译文将在此实时展现': ('Paragraphs and their translations will appear here as the document is processed.', '翻訳開始後、各段落と翻訳がここに表示されます。', '번역을 시작하면 각 문단과 번역문이 여기에 표시됩니다.'),
'正文段落': ('Paragraph', '本文', '본문'), '标题 / 图注': ('Title / caption', '見出し / キャプション', '제목 / 캡션'), '⚡ 复用缓存': ('⚡ Cached', '⚡ キャッシュ', '⚡ 캐시 사용'), '✓ 校验通过': ('✓ Validated', '✓ 検証済み', '✓ 검증 완료'), '⚓ 锚点修复保全': ('⚓ Anchors repaired', '⚓ アンカーを修復', '⚓ 앵커 복구'), '✕ 安全回退原文': ('✕ Original retained', '✕ 原文を保持', '✕ 원문 유지'),
'正在准备本地推理引擎...': ('Preparing local inference engine...', 'ローカル推論エンジンを準備中...', '로컬 추론 엔진 준비 중...'), '正在安全中止当前任务与推理服务...': ('Stopping the current task and engine...', 'タスクとエンジンを停止中...', '작업과 엔진을 중지하는 중...'), '批量处理已手动停止。': ('Batch stopped.', '一括処理を停止しました。', '일괄 처리가 중지되었습니다.'), '运行中断': ('Interrupted', '処理中断', '실행 중단'), '正在停止，结束后将关闭窗口': ('Stopping; the window will close when finished.', '停止中です。完了後にウィンドウを閉じます。', '중지 중입니다. 완료되면 창이 닫힙니다.'),
'选择待翻译学术 PDF 文献': ('Select PDFs to translate', '翻訳するPDFを選択', '번역할 PDF 선택'), 'PDF 文件 (*.pdf);;所有文件 (*.*)': ('PDF files (*.pdf);;All files (*.*)', 'PDFファイル (*.pdf);;すべてのファイル (*.*)', 'PDF 파일 (*.pdf);;모든 파일 (*.*)'), '执行失败': ('Execution failed', '実行失敗', '실행 실패'),
'处理中': ('Processing', '処理中', '처리 중'), '等待中': ('Waiting', '待機中', '대기 중'), '已完成': ('Completed', '完了', '완료'), '启动引擎': ('Starting engine', 'エンジン起動中', '엔진 시작 중'), '失败': ('Failed', '失敗', '실패'),
'解析文档': ('Parsing document', '文書を解析中', '문서 분석 중'), '扫描检测': ('Detecting scans', 'スキャンを検出中', '스캔 감지 중'), '版面解析': ('Analyzing layout', 'レイアウト解析中', '레이아웃 분석 중'), '表格解析': ('Parsing tables', '表を解析中', '표 분석 중'), '段落切分': ('Splitting paragraphs', '段落を分割中', '문단 분할 중'), '公式解析': ('Parsing formulas', '数式を解析中', '수식 분석 중'), '提取术语': ('Extracting terms', '用語を抽出中', '용어 추출 중'), '段落翻译': ('Translating', '翻訳中', '번역 중'), '排版渲染': ('Typesetting', '組版中', '조판 중'), '嵌入字体': ('Embedding fonts', 'フォントを埋め込み中', '글꼴 삽입 중'), '生成图元': ('Generating graphics', '図形を生成中', '그래픽 생성 중'), '精简字体': ('Subsetting fonts', 'フォントを最適化中', '글꼴 최적화 중'), '保存输出': ('Saving', '保存中', '저장 중'),
}

_TEXT.update({
    '仅显示最近 100 条': ('Showing the latest 100 blocks', '最新の100件を表示', '최근 100개만 표시'),
    '该语言的 PDF 文字方向或组合可能不正确，生成后请检查。': ('PDF text direction or shaping may be incorrect for this language. Please check the result.', 'この言語のPDFでは文字方向や組版が正しくない場合があります。結果を確認してください。', '이 언어의 PDF는 글자 방향이나 조합이 잘못될 수 있습니다. 결과를 확인하세요.'),
    'ORIGINAL': ('Original', '原文', '원문'),
    'TRANSLATION': ('Translation', '翻訳', '번역'),
    '处理过程中发生严重错误:': ('A serious error occurred:', '処理中にエラーが発生しました:', '처리 중 오류가 발생했습니다:'),
    '详细信息请查看日志': ('See the log for details', '詳細はログを確認してください', '자세한 내용은 로그를 확인하세요'),
    '文献翻译': ('PDF Docs', 'PDF文書翻訳', 'PDF 문서 번역'),
    '日常翻译': ('Daily Translate', '日常翻訳', '일상 번역'),
    '智能互译 (中 ⇄ 英)': ('Smart Auto (ZH ⇄ EN)', 'スマート相互翻訳 (中 ⇄ 英)', '스마트 상호 번역 (중 ⇄ 영)'),
    '自动互译 (中 ⇄ 英)': ('Auto (ZH ⇄ EN)', '自動相互翻訳 (中 ⇄ 英)', '자동 상호 번역 (중 ⇄ 영)'),
    '风格:': ('Tone:', 'スタイル:', '어조:'),
    '标准默认': ('Standard', '標準', '기본'),
    '学术严谨': ('Academic', '学術・技術', '학술·전문'),
    '商务正式': ('Formal', 'ビジネス敬語', '비즈니스 격식'),
    '自然口语': ('Casual', '自然な口語', '자연스러운 구어'),
    '精简地道': ('Concise', '簡潔・自然', '간결·자연'),
    '默认': ('Default', '標準', '기본'),
    '学术': ('Academic', '学術', '학술'),
    '正式': ('Formal', 'ビジネス', '격식'),
    '口语': ('Casual', '口語', '구어'),
    '精简': ('Concise', '簡潔', '간결'),
    '术语表': ('Glossary', '用語集', '용어집'),
    '历史记录': ('History', '履歴', '기록'),
    '原文': ('Source', '原文', '원문'),
    '译文': ('Translation', '訳文', '번역'),
    '粘贴并翻译': ('Paste & Translate', '貼り付けて翻訳', '붙여넣고 번역'),
    '整理PDF断行': ('Unwrap PDF Lines', 'PDF改行を整理', 'PDF 줄바꿈 정리'),
    '整理换行': ('Fix line breaks', '改行を整理', '줄바꿈 정리'),
    '合并复制文本中多余的换行': ('Merge extra line breaks in pasted text', '貼り付けたテキストの不要な改行を整理', '붙여넣은 텍스트의 불필요한 줄바꿈 정리'),
    '清空': ('Clear', 'クリア', '지우기'),
    '自动翻译': ('Auto translate', '自動翻訳', '자동 번역'),
    '监听剪贴板': ('Clipboard watch', 'クリップボード監視', '클립보드 감시'),
    '自动复制': ('Auto copy', '自動コピー', '자동 복사'),
    '翻译': ('Translate', '翻訳', '번역'),
    '+ 设为术语': ('+ Add Term', '+ 用語登録', '+ 용어 추가'),
    '复制译文': ('Copy', 'コピー', '복사'),
    '✓ 已复制': ('✓ Copied', '✓ コピー済み', '✓ 복사됨'),
    '启用术语表约束': ('Enable glossary enforcement', '用語集の強制適用を有効化', '용어집 강제 적용'),
    '翻译时启用术语表': ('Use glossary', '用語集を使用', '용어집 사용'),
    '添加': ('Add', '追加', '추가'),
    '导入': ('Import', 'インポート', '가져오기'),
    '导出': ('Export', 'エクスポート', '내보내기'),
    '清空历史': ('Clear history', '履歴を消去', '기록 지우기'),
    '释放模型内存': ('Unload model', 'メモリ解放', '모델 메모리 해제'),
    '直接输入或粘贴任意内容，停顿即自动翻译 · 支持拖入 TXT/MD 文件 · 代码块自动保留': ('Type or paste text to auto-translate · Drop TXT/MD files · Code blocks preserved', 'テキストを入力・貼り付けで自動翻訳 · TXT/MDドロップ対応 · コードブロック保持', '텍스트를 입력하거나 붙여넣으면 자동 번역 · TXT/MD 드롭 지원 · 코드 블록 보존'),
    '输入或粘贴需要翻译的内容': ('Enter or paste text to translate', '翻訳するテキストを入力または貼り付け', '번역할 내용을 입력하거나 붙여넣으세요'),
    '译文将在此处实时流式输出...': ('Translation will stream here...', '翻訳結果がここにリアルタイム表示されます...', '번역 결과가 여기에 실시간으로 표시됩니다...'),
    '译文将显示在这里': ('Translation will appear here', '翻訳結果がここに表示されます', '번역 결과가 여기에 표시됩니다'),
    '原文术语': ('Source term', '原語', '원본 용어'),
    '目标译词': ('Target term', '訳語', '대상 역어'),
    '原文词汇': ('Source term', '原文', '원문'),
    '指定译文': ('Translation', '訳語', '번역'),
    '搜索术语...': ('Search glossary...', '用語を検索...', '용어 검색...'),
    '搜索历史...': ('Search history...', '履歴を検索...', '기록 검색...'),
    '回填': ('Use', '読込', '불러오기'),
    '填入': ('Use', '入力', '불러오기'),
    '复制': ('Copy', 'コピー', '복사'),
    '暂无术语': ('No glossary terms yet', '登録された用語はありません', '등록된 용어가 없습니다'),
    '暂无翻译记录': ('No translation history yet', '翻訳履歴はありません', '번역 기록이 없습니다'),
    '准备中...': ('Preparing...', '準備中...', '준비 중...'),
    '翻译输出中...': ('Translating...', '翻訳出力中...', '번역 출력 중...'),
    '翻译完成': ('Completed', '翻訳完了', '번역 완료'),
    '翻译失败': ('Failed', '翻訳失敗', '번역 실패'),
    '翻译未能完成，请重试。': ('Translation failed. Please try again.', '翻訳を完了できませんでした。もう一度お試しください。', '번역을 완료하지 못했습니다. 다시 시도해 주세요.'),
    '离线翻译 · 待命': ('Offline translation · Standby', 'オフライン翻訳 · 待機中', '오프라인 번역 · 대기 중'),
    '正在准备翻译服务...': ('Preparing translation service...', '翻訳サービスを準備中...', '번역 서비스 준비 중...'),
    '离线翻译 · 已就绪': ('Offline translation · Ready', 'オフライン翻訳 · 準備完了', '오프라인 번역 · 준비됨'),
    '本地模型待命（首次翻译自动唤醒，后续秒级响应）': ('Local model standby (wakes on first translate, instant thereafter)', 'ローカルモデル待機中（初回のみ起動、以降は高速応答）', '로컬 모델 대기 중 (첫 번역 시 자동 실행, 이후 즉시 응답)'),
    '● 本地引擎已就绪 · 连续翻译零等待': ('● Local engine ready · Instant response', '● ローカルエンジン稼働中 · 即時応答', '● 로컬 엔진 준비 완료 · 즉시 응답'),
    'PDF 文献翻译正在后台运行中，为保证系统稳定性，请在 PDF 任务完成或停止后使用日常翻译。': ('PDF translation is currently running in the background. Please wait until it finishes or stops before using Daily Translate.', 'PDF翻訳がバックグラウンドで実行中です。完了または停止後に日常翻訳をご利用ください。', 'PDF 번역이 백그라운드에서 실행 중입니다. 완료되거나 중지된 후 일상 번역을 사용해 주세요.'),
    '正在翻译 PDF 文档，请等待完成或停止后再使用日常翻译。': ('A PDF is currently being translated. Please wait until it finishes or stop it first.', 'PDF文書を翻訳中です。完了または停止してから日常翻訳をご利用ください。', 'PDF 문서를 번역 중입니다. 완료되거나 중지된 후 일상 번역을 이용해 주세요.'),
    '唤醒模型...': ('Waking model...', 'モデルを起動中...', '모델 실행 중...'),
    '正在唤醒本地模型 Hy-MT2-1.8B（首次约需数秒）...': ('Waking local model Hy-MT2-1.8B (a few seconds on first run)...', 'ローカルモデル Hy-MT2-1.8B を起動中（初回は数秒かかります）...', '로컬 모델 Hy-MT2-1.8B 실행 중(최초 몇 초 소요)...'),
    'Wayland 桌面环境下系统可能限制后台应用读取剪贴板，若未自动触发请使用“粘贴并翻译”。': ('Wayland may restrict background clipboard access. Use "Paste & Translate" if watch does not trigger.', 'Wayland環境ではバックグラウンドのクリップボード読み取りが制限される場合があります。反応しない場合は「貼り付けて翻訳」をご利用ください。', 'Wayland 환경에서는 백그라운드 클립보드 읽기가 제한될 수 있습니다. 자동 감지되지 않으면 "붙여넣고 번역"을 사용하세요.'),
    '将文件拖放到此处，或点击下方按钮选择 · 本地离线翻译': ('Drop PDF files here or select below · Offline local translation', 'ここにファイルをドロップ、または下のボタンで選択 · オフライン翻訳', '여기에 파일을 놓거나 아래 버튼으로 선택 · 오프라인 번역'),
    '自动配置': ('Auto config', '自動設定', '자동 설정'),
    '开始翻译后，文档中每个段落的原文和译文将在此实时显示': ('Paragraphs and their translations will appear here as the document is translated.', '翻訳開始後、各段落の原文と訳文がここに表示されます。', '번역을 시작하면 각 문단의 원문과 번역문이 여기에 표시됩니다.'),
    '正在停止翻译...': ('Stopping translation...', '翻訳を停止中...', '번역을 중지하는 중...'),
    '已停止翻译': ('Translation stopped', '翻訳を停止しました', '번역이 중지되었습니다'),
    '准备中': ('Preparing', '準備中', '준비 중'),
    '复用已有译文': ('Reused', '再利用', '재사용'),
    '✓ 已完成': ('✓ Done', '✓ 完了', '✓ 완료'),
    '✓ 重新翻译完成': ('✓ Retranslated', '✓ 再翻訳完了', '✓ 재번역 완료'),
    '重新翻译中...': ('Retranslating...', '再翻訳中...', '재번역 중...'),
    '已保留格式': ('Format kept', '書式保持', '서식 유지'),
    '保留原文': ('Original kept', '原文を保持', '원문 유지'),
    '↓ 回到最新': ('↓ Latest', '↓ 最新へ', '↓ 최신으로'),
    '已暂停自动跟随，点击回到最新输出': ('Auto-scroll paused. Click to jump to the latest output.', '自動スクロールを一時停止中。クリックで最新出力へ移動します。', '자동 스크롤이 일시 중지되었습니다. 클릭하여 최신 출력으로 이동합니다.'),
    '输出可能不完整': ('Output may be incomplete', '出力不完全の可能性', '출력이 불완전할 수 있음'),
    '部分内容受模型单次长度限制未能完整输出，建议缩短原文或分段翻译。': ('Some text was truncated by the model length limit. Shorten the input or translate in smaller parts.', 'モデルの出力上限により一部が途中で切れた可能性があります。原文を短くするか分割して翻訳してください。', '모델 길이 제한으로 일부 내용이 잘렸을 수 있습니다. 원문을 줄이거나 나누어 번역하세요.'),
    '译文已达单次输出上限，后续内容已停止生成，建议分段翻译。': ('Output reached the single-run limit and stopped generating. Please translate in smaller parts.', '出力が1回の上限に達したため生成を停止しました。分割して翻訳してください。', '출력이 1회 한도에 도달하여 생성을 중단했습니다. 나누어 번역하세요.'),
    '翻译已手动停止，当前仅显示已完成部分，并非完整译文。': ('Translation stopped manually. Only the generated part is shown.', '翻訳を手動停止しました。生成済みの部分のみ表示しています。', '번역이 수동 중지되어 생성된 일부만 표시됩니다.'),
})

_RU = {'OfflinePDF Translate — 本地离线PDF文献翻译': 'OfflinePDF Translate — автономный перевод PDF', '+ 添加文献': '+ Добавить PDF', '清空队列': 'Очистить очередь', '文献任务': 'Документы', '展开翻译流': 'Показать перевод', '收起翻译流': 'Скрыть перевод', '添加待翻译 PDF 文献': 'Добавьте PDF для перевода', '将文件拖放到此处，或点击按钮选取 · 纯本地大模型离线处理': 'Перетащите файлы сюда или выберите их ниже · Автономная обработка', '选择本地文件': 'Выбрать файлы', '源语言:': 'Исходный:', '自动识别': 'Авто', '目标语言:': 'Язык перевода:', '自动硬件': 'Автонастройка', '线程:': 'Потоки:', '并发:': 'Параллельно:', '窗口:': 'Контекст:', 'GPU 层数:': 'Слои GPU:', '停止': 'Остановить', '开始翻译': 'Начать перевод', '对照译本': 'Двуязычный PDF', '单语译本': 'Переведённый PDF', '打开目录': 'Открыть папку', '待执行': 'Ожидание', '排队中': 'В очереди', '完成': 'Готово', '已停止': 'Остановлено', '需复核': 'Нужна проверка', '处理失败': 'Ошибка обработки', '实时翻译流': 'Перевод в реальном времени', '等待任务启动': 'Ожидание запуска', '↑ 回到最新': '↑ К последним', '● 返回实时流': '● К текущему переводу', '已暂停自动置顶，点击立即回到最新实时翻译段落': 'Автопрокрутка приостановлена. Нажмите, чтобы перейти к последнему тексту.', '后台正在翻译另一篇文献，点击切回正在进行的实时翻译流': 'Переводится другой документ. Нажмите, чтобы вернуться к нему.', '实时翻译流待启动': 'Ожидание перевода', '启动文献翻译后，模型解析出的每一个段落及其即时译文将在此实时展现': 'После запуска здесь появятся абзацы и их перевод.', '正文段落': 'Абзац', '标题 / 图注': 'Заголовок / подпись', '⚡ 复用缓存': '⚡ Из кэша', '✓ 校验通过': '✓ Проверено', '⚓ 锚点修复保全': '⚓ Опорные элементы восстановлены', '✕ 安全回退原文': '✕ Сохранён оригинал', '正在准备本地推理引擎...': 'Подготовка локального движка...', '正在安全中止当前任务与推理服务...': 'Остановка задачи и движка...', '批量处理已手动停止。': 'Пакетная обработка остановлена.', '运行中断': 'Обработка прервана', '正在停止，结束后将关闭窗口': 'Остановка; окно закроется после завершения.', '选择待翻译学术 PDF 文献': 'Выберите PDF для перевода', 'PDF 文件 (*.pdf);;所有文件 (*.*)': 'Файлы PDF (*.pdf);;Все файлы (*.*)', '执行失败': 'Ошибка выполнения', '处理中': 'Обработка', '等待中': 'Ожидание', '已完成': 'Завершено', '启动引擎': 'Запуск движка', '失败': 'Ошибка', '解析文档': 'Разбор документа', '扫描检测': 'Проверка на скан', '版面解析': 'Анализ разметки', '表格解析': 'Разбор таблиц', '段落切分': 'Разбиение на абзацы', '公式解析': 'Разбор формул', '提取术语': 'Извлечение терминов', '段落翻译': 'Перевод абзацев', '排版渲染': 'Вёрстка', '嵌入字体': 'Встраивание шрифтов', '生成图元': 'Создание графики', '精简字体': 'Оптимизация шрифтов', '保存输出': 'Сохранение'}

_RU.update({'该语言的 PDF 文字方向或组合可能不正确，生成后请检查。': 'Направление или форма символов в PDF могут отображаться неверно. Проверьте результат.', '仅显示最近 100 条': 'Показаны последние 100 фрагментов', 'ORIGINAL': 'Оригинал', 'TRANSLATION': 'Перевод',
            '处理过程中发生严重错误:': 'Во время обработки произошла ошибка:',
            '详细信息请查看日志': 'Подробности в журнале',
            '文献翻译': 'PDF-документы',
            '日常翻译': 'Быстрый перевод',
            '智能互译 (中 ⇄ 英)': 'Авто (中 ⇄ EN)',
            '自动互译 (中 ⇄ 英)': 'Авто (中 ⇄ EN)',
            '风格:': 'Стиль:',
            '标准默认': 'Стандарт',
            '学术严谨': 'Академический',
            '商务正式': 'Деловой',
            '自然口语': 'Разговорный',
            '精简地道': 'Краткий',
            '默认': 'Обычный',
            '学术': 'Академический',
            '正式': 'Деловой',
            '口语': 'Разговорный',
            '精简': 'Краткий',
            '术语表': 'Глоссарий',
            '历史记录': 'История',
            '原文': 'Исходный текст',
            '译文': 'Перевод',
            '粘贴并翻译': 'Вставить и перевести',
            '整理PDF断行': 'Убрать переносы PDF',
            '整理换行': 'Убрать переносы',
            '合并复制文本中多余的换行': 'Объединить лишние переносы строк',
            '清空': 'Очистить',
            '自动翻译': 'Автоперевод',
            '监听剪贴板': 'Буфер обмена',
            '自动复制': 'Автокопирование',
            '翻译': 'Перевести',
            '+ 设为术语': '+ В глоссарий',
            '复制译文': 'Копировать',
            '✓ 已复制': '✓ Скопировано',
            '启用术语表约束': 'Применять глоссарий',
            '翻译时启用术语表': 'Использовать глоссарий',
            '添加': 'Добавить',
            '导入': 'Импорт',
            '导出': 'Экспорт',
            '清空历史': 'Очистить историю',
            '释放模型内存': 'Выгрузить модель',
            '直接输入或粘贴任意内容，停顿即自动翻译 · 支持拖入 TXT/MD 文件 · 代码块自动保留': 'Введите или вставьте текст для автоперевода · Поддержка TXT/MD · Блоки кода сохраняются',
            '输入或粘贴需要翻译的内容': 'Введите или вставьте текст для перевода',
            '译文将在此处实时流式输出...': 'Перевод появится здесь...',
            '译文将显示在这里': 'Здесь появится перевод',
            '原文术语': 'Исходный термин',
            '目标译词': 'Перевод термина',
            '原文词汇': 'Оригинал',
            '指定译文': 'Перевод',
            '搜索术语...': 'Поиск терминов...',
            '搜索历史...': 'Поиск в истории...',
            '回填': 'Вставить',
            '填入': 'Вставить',
            '复制': 'Копировать',
            '暂无术语': 'Терминов пока нет',
            '暂无翻译记录': 'История переводов пуста',
            '准备中...': 'Подготовка...',
            '翻译输出中...': 'Перевод...',
            '翻译完成': 'Перевод завершён',
            '翻译失败': 'Ошибка перевода',
            '翻译未能完成，请重试。': 'Не удалось выполнить перевод. Повторите попытку.',
            '离线翻译 · 待命': 'Офлайн-перевод · Ожидание',
            '正在准备翻译服务...': 'Подготовка службы перевода...',
            '离线翻译 · 已就绪': 'Офлайн-перевод · Готов',
            '本地模型待命（首次翻译自动唤醒，后续秒级响应）': 'Локальная модель готова (запуск при первом переводе)',
            '● 本地引擎已就绪 · 连续翻译零等待': '● Движок готов · Мгновенный отклик',
            'PDF 文献翻译正在后台运行中，为保证系统稳定性，请在 PDF 任务完成或停止后使用日常翻译。': 'Идёт фоновый перевод PDF. Дождитесь завершения или остановите задачу PDF.',
            '正在翻译 PDF 文档，请等待完成或停止后再使用日常翻译。': 'Идёт перевод PDF. Дождитесь завершения или остановите задачу.',
            '唤醒模型...': 'Запуск модели...',
            '正在唤醒本地模型 Hy-MT2-1.8B（首次约需数秒）...': 'Запуск локальной модели Hy-MT2-1.8B (несколько секунд при первом запуске)...',
            'Wayland 桌面环境下系统可能限制后台应用读取剪贴板，若未自动触发请使用“粘贴并翻译”。': 'В среде Wayland доступ к буферу обмена в фоне может быть ограничен. Используйте «Вставить и перевести».',
            '将文件拖放到此处，或点击下方按钮选择 · 本地离线翻译': 'Перетащите PDF сюда или выберите ниже · Офлайн-перевод',
            '自动配置': 'Автонастройка',
            '开始翻译后，文档中每个段落的原文和译文将在此实时显示': 'После запуска здесь появятся абзацы и их перевод.',
            '正在停止翻译...': 'Остановка перевода...',
            '已停止翻译': 'Перевод остановлен',
            '准备中': 'Подготовка',
            '复用已有译文': 'Повторно',
            '✓ 已完成': '✓ Готово',
            '✓ 重新翻译完成': '✓ Переведено повторно',
            '重新翻译中...': 'Повторный перевод...',
            '已保留格式': 'Формат сохранён',
            '保留原文': 'Сохранён оригинал',
            '↓ 回到最新': '↓ К новым',
            '已暂停自动跟随，点击回到最新输出': 'Автопрокрутка приостановлена. Нажмите, чтобы перейти к новому выводу.',
            '输出可能不完整': 'Вывод может быть неполным',
            '部分内容受模型单次长度限制未能完整输出，建议缩短原文或分段翻译。': 'Часть текста могла быть обрезана из-за ограничения длины модели. Сократите текст или переводите частями.',
            '译文已达单次输出上限，后续内容已停止生成，建议分段翻译。': 'Достигнут предел вывода за один запуск; генерация остановлена. Переводите частями.',
            '翻译已手动停止，当前仅显示已完成部分，并非完整译文。': 'Перевод остановлен вручную. Показана только уже выполненная часть.'})

def detail(text):
    """Keep arbitrary engine errors intact in Chinese; refer to the log otherwise."""
    import re
    if _language != 'zh' and re.search(r'[\u4e00-\u9fff]', text or ''):
        return tr('详细信息请查看日志')
    return text

# Stable identifiers for primary controls; translations can change independently
# of the widgets that use them. Legacy dynamic status strings remain supported.
UI_KEYS = {
    'ui.add': '+ 添加文献', 'ui.clear': '清空队列', 'ui.documents': '文献任务',
    'ui.show_live': '展开翻译流', 'ui.hide_live': '收起翻译流',
    'ui.add_pdfs': '添加待翻译 PDF 文献',
    'ui.drop_hint': '将文件拖放到此处，或点击下方按钮选择 · 本地离线翻译',
    'ui.select': '选择本地文件', 'ui.source': '源语言:', 'ui.target': '目标语言:', 'ui.auto_hardware': '自动配置',
    'ui.threads': '线程:', 'ui.parallel': '并发:', 'ui.context': '窗口:', 'ui.gpu_layers': 'GPU 层数:',
    'ui.stop': '停止', 'ui.start': '开始翻译', 'ui.bilingual': '对照译本',
    'ui.translated': '单语译本', 'ui.open_folder': '打开目录',
    'ui.live': '实时翻译流', 'ui.wait_start': '等待任务启动',
    'ui.latest': '↑ 回到最新', 'ui.back_live': '● 返回实时流',
    'ui.live_ready': '实时翻译流待启动',
    'ui.live_hint': '开始翻译后，文档中每个段落的原文和译文将在此实时显示',
    'ui.original': 'ORIGINAL', 'ui.translation': 'TRANSLATION',
}

# Stable message identifiers for error guidance, independent of engine wording.
_MESSAGES = {
    'missing': ('找不到文件，请重新添加后重试。', 'File not found. Add it again and retry.', 'ファイルが見つかりません。追加し直してください。', '파일을 찾을 수 없습니다. 다시 추가해 주세요.', 'Файл не найден. Добавьте его снова.'),
    'scan': ('不支持扫描版的 PDF，请先进行文字识别（OCR）后再重试。', 'Scanned PDFs are not supported. Run OCR first, then retry.', 'スキャン版PDFには対応していません。OCR処理を行ってから再試行してください。', '스캔된 PDF는 지원되지 않습니다. OCR 처리 후 다시 시도하세요.', 'Сканированные PDF не поддерживаются. Сначала выполните OCR и повторите попытку.'),
    'memory': ('可用内存不足，请关闭其他程序后重试。', 'Not enough memory. Close other apps and retry.', 'メモリ不足です。他のアプリを閉じて再試行してください。', '메모리가 부족합니다. 다른 앱을 닫고 다시 시도하세요.', 'Недостаточно памяти. Закройте другие программы и повторите попытку.'),
    'assets': ('程序文件不完整，请重新安装后重试。', 'Application files are incomplete. Reinstall and retry.', 'プログラムファイルが不足しています。再インストールしてください。', '프로그램 파일이 불완전합니다. 다시 설치해 주세요.', 'Файлы программы повреждены. Переустановите программу.'),
    'permission': ('无法写入文件，请检查目标文件夹权限或关闭已打开的 PDF。', 'Cannot write the output. Check folder permissions or close the PDF.', '出力できません。フォルダーの権限を確認し、PDFを閉じてください。', '파일을 쓸 수 없습니다. 폴더 권한을 확인하거나 PDF를 닫으세요.', 'Не удалось записать файл. Проверьте права на папку или закройте PDF.'),
    'invalid': ('无法读取此 PDF，请确认文件未损坏或加密。', 'Cannot read this PDF. Check whether it is damaged or encrypted.', 'PDFを読み取れません。破損や暗号化を確認してください。', 'PDF를 읽을 수 없습니다. 파일 손상 또는 암호화를 확인하세요.', 'Не удалось прочитать PDF. Проверьте, не повреждён ли он и не защищён ли паролем.'),
    'timeout': ('处理超时，请关闭其他程序后重试。', 'Processing timed out. Close other apps and retry.', '処理がタイムアウトしました。他のアプリを閉じて再試行してください。', '처리 시간이 초과되었습니다. 다른 앱을 닫고 다시 시도하세요.', 'Время обработки истекло. Закройте другие программы и повторите попытку.'),
    'review': ('部分内容翻译未通过检查，请点击开始重试。', 'Some text did not pass validation. Start again to retry.', '一部の翻訳が検証に失敗しました。再度開始してください。', '일부 번역이 검증에 실패했습니다. 다시 시작해 주세요.', 'Часть перевода не прошла проверку. Запустите повторно.'),
    'context_limit': ('输入内容超出模型单次处理容量，请缩短文本或分段翻译。', 'Input exceeds the model context limit. Shorten the text or translate in smaller parts.', '入力がモデルのコンテキスト上限を超えています。テキストを短くするか分割して翻訳してください。', '입력이 모델 컨텍스트 한도를 초과했습니다. 텍스트를 줄이거나 나누어 번역하세요.', 'Входной текст превышает лимит контекста модели. Сократите текст или переводите частями.'),
    'input_too_long': ('输入字数超出单次上限，请精简内容或分段翻译。', 'Input exceeds the single-request character limit. Please shorten or split the text.', '文字数が1回の上限を超えています。内容を減らすか分割して翻訳してください。', '입력 글자 수가 1회 한도를 초과했습니다. 내용을 줄이거나 나누어 번역하세요.', 'Превышен лимит символов для одного перевода. Сократите текст или переводите частями.'),
    'general': ('处理未完成，请点击开始重试；如仍失败，请查看日志。', 'Processing failed. Retry; if it fails again, check the log.', '処理できませんでした。再試行し、再び失敗する場合はログを確認してください。', '처리하지 못했습니다. 다시 시도하고 문제가 계속되면 로그를 확인하세요.', 'Обработка не завершена. Повторите попытку; при повторной ошибке проверьте журнал.'),
}

def message(key):
    return _MESSAGES[key][{'zh': 0, 'en': 1, 'ja': 2, 'ko': 3, 'ru': 4}[_language]]

def error_key(error, status='error'):
    text = (error or '').lower()
    if '文件不存在' in text or 'no such file' in text: return 'missing'
    if '扫描' in text or 'scanned' in text or 'no paragraphs' in text: return 'scan'
    if 'memory' in text or '内存' in text or 'out of memory' in text: return 'memory'
    if '资产' in text or 'model file not found' in text or '运行时不存在' in text: return 'assets'
    if 'permission' in text or '拒绝访问' in text or '权限' in text: return 'permission'
    if '加密' in text or 'encrypted' in text or 'invalid pdf' in text or '损坏' in text: return 'invalid'
    if '超时' in text or 'timeout' in text or 'timed out' in text: return 'timeout'
    if '上下文' in text or 'exceed_context_size' in text or 'n_ctx' in text: return 'context_limit'
    if '超出单次' in text or 'input_too_long' in text: return 'input_too_long'
    if status == 'review': return 'review'
    return 'general'

def detect_system_language():
    """Detect the OS UI language and map it to a supported interface code, defaulting to English."""
    candidates = []
    try:
        candidates.extend(QLocale.system().uiLanguages() or [])
    except Exception:
        pass
    try:
        name = QLocale.system().name()
        if name:
            candidates.append(name)
    except Exception:
        pass

    for raw in candidates:
        tag = str(raw or '').strip().lower().replace('_', '-')
        if not tag:
            continue
        primary = tag.split('-')[0]
        if primary in ('zh', 'ja', 'ko', 'ru', 'en'):
            return primary
    return 'en'


_SETTINGS_ORG = 'OfflinePDF'
_SETTINGS_APP = 'Translate'


def set_language(language):
    global _language
    supported = {code for _, code in LANGUAGES}
    _language = language if language in supported else detect_system_language()
    QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue('interface_language', _language)


def saved_language():
    supported = {code for _, code in LANGUAGES}
    value = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value('interface_language', None)
    if value in supported:
        return value
    return detect_system_language()

def tr(text):
    if isinstance(text, str):
        text = UI_KEYS.get(text, text)
    if _language == 'zh' or not isinstance(text, str):
        return text
    index = {'en': 0, 'ja': 1, 'ko': 2, 'ru': 3}[_language]
    if text.startswith('诊断详情: '):
        return ('Details: ', '詳細: ', '상세: ', 'Подробности: ')[index] + detail(text[len('诊断详情: '):])
    if text.startswith('错误详情: '):
        return detail(text[len('错误详情: '):])
    if text in _TEXT:
        return _RU[text] if _language == 'ru' else _TEXT[text][index]
    # Translate UI-generated status fragments while retaining values and filenames.
    import re
    patterns = [
        (r'^正在处理第 (\d+)/(\d+) 篇文献 \((\d+)%\)$', ('Processing document {0}/{1} ({2}%)', '文書 {0}/{1} を処理中 ({2}%)', '문서 {0}/{1} 처리 중 ({2}%)', 'Документ {0}/{1} ({2}%)')),
        (r'^处理结束：成功 (\d+)，失败 (\d+)，需复核 (\d+)$', ('Finished: {0} succeeded, {1} failed, {2} need review', '完了: 成功 {0}、失敗 {1}、要確認 {2}', '완료: 성공 {0}, 실패 {1}, 검토 필요 {2}', 'Завершено: успешно {0}, ошибок {1}, проверить {2}')),
        (r'^\(推荐: (\d+)核/(\d+)槽/(\d+)窗口\)$', ('(Recommended: {0} cores / {1} slots / {2} context)', '(推奨: {0}コア / {1}並列 / {2}コンテキスト)', '(권장: {0}코어 / {1}병렬 / {2} 컨텍스트)', '(Рекомендуется: {0} ядер / {1} потоков / контекст {2})')),
        (r'^\(推荐: (\d+)核/(\d+)槽/(\d+)窗口/(\d+)层\)$', ('(Recommended: {0} cores / {1} slots / {2} ctx / {3} GPU layers)', '(推奨: {0}コア / {1}並列 / {2}コンテキスト / GPU {3}層)', '(권장: {0}코어 / {1}병렬 / {2} 컨텍스트 / GPU {3}레이어)', '(Рекомендуется: {0} ядер / {1} потоков / контекст {2} / {3} слоёв GPU)')),
        (r'^✓ 纠错重试通过 \(第(\d+)次\)$', ('✓ Retry passed (attempt {0})', '✓ 再試行成功 ({0}回目)', '✓ 재시도 성공 ({0}회)', '✓ Повторная проверка пройдена (попытка {0})')),
        (r'^⚠ 重试纠错中 \(第(\d+)次\)$', ('⚠ Retrying (attempt {0})', '⚠ 再試行中 ({0}回目)', '⚠ 재시도 중 ({0}회)', '⚠ Повторная попытка {0}')),
        (r'^(\d+) 字$', ('{0} chars', '{0} 文字', '{0}자', '{0} симв.')),
        (r'^(\d+) / (\d+) 字$', ('{0} / {1} chars', '{0} / {1} 文字', '{0} / {1}자', '{0} / {1} симв.')),
        (r'^保留前 (\d+) 字$', ('Keep first {0}', '先頭 {0} 文字を残す', '앞 {0}자만 유지', 'Оставить {0}')),
        (r'^已超出 (\d+) 字（单次上限 (\d+) 字），请精简或分段翻译$', ('{0} chars over the {1}-char limit. Shorten or translate in parts.', '{0} 文字超過しています（上限 {1} 文字）。分割して翻訳してください。', '한도({1}자)를 {0}자 초과했습니다. 줄이거나 나누어 번역하세요.', 'Превышение на {0} симв. (лимит {1}). Сократите текст или переводите частями.')),
        (r'^单次最多支持 (\d+) 字，超出请分段翻译$', ('Up to {0} chars per translation; split longer text into parts.', '1回あたり最大 {0} 文字まで対応。長い場合は分割してください。', '한 번에 최대 {0}자까지 지원합니다. 초과 시 나누어 번역하세요.', 'До {0} символов за один перевод; длинный текст переводите частями.')),
        (r'^单次最多输出 (\d+) 字$', ('Up to {0} output chars per run', '1回あたり最大 {0} 文字まで出力', '1회 최대 {0}자 출력', 'Максимум {0} символов вывода')),
        (r'^(\d+) 术语$', ('{0} terms', '{0} 用語', '{0}개 용어', '{0} терм.')),
        (r'^翻译输出中 (\d+)%$', ('Translating {0}%', '翻訳出力中 {0}%', '번역 출력 중 {0}%', 'Перевод {0}%')),
    ]
    for pattern, translations in patterns:
        match = re.match(pattern, text)
        if match:
            return translations[index].format(*match.groups())
    result = text
    for prefix, translations in {
        '✓ 耗时 ': ('✓ Elapsed ', '✓ 経過 ', '✓ 경과 ', '✓ Время '),
        '中断 · ': ('Interrupted · ', '中断 · ', '중단 · ', 'Прервано · '),
        '已停止 · ': ('Stopped · ', '停止済み · ', '중지됨 · ', 'Остановлено · '),
        '诊断详情: ': ('Details: ', '詳細: ', '상세: ', 'Подробности: '),
        '画像: ': ('Profile: ', 'プロファイル: ', '프로필: ', 'Профиль: '),
        '↑ 回到最新': ('↑ Latest', '↑ 最新へ', '↑ 최신으로', '↑ К последним'),
    }.items():
        if result.startswith(prefix):
            result = translations[index] + result[len(prefix):]
            break
    if result.endswith('待执行'):
        pending = _RU['待执行'] if _language == 'ru' else _TEXT['待执行'][index]
        result = result[:-3] + pending
    for source in sorted(_TEXT, key=len, reverse=True):
        if result.startswith(source + ':') or result.startswith(source + ' ') or result.startswith(source + ' ·'):
            result = (_RU[source] if _language == 'ru' else _TEXT[source][index]) + result[len(source):]
            break
    if text.startswith('画像: '):
        for source, translations in {
            '线程': ('threads', 'スレッド', '스레드', 'потоков'),
            '可用内存': ('Available RAM', '利用可能メモリ', '사용 가능 메모리', 'Доступная память'),
            '显存': ('VRAM', 'VRAM', 'VRAM', 'видеопамять'),
            '层数': ('layers', 'レイヤー', '레이어', 'слои'),
            '槽位': ('Slots', '並列数', '슬롯', 'Слоты'),
            '窗口': ('context', 'コンテキスト', '컨텍스트', 'контекст'),
            '当前运行时无对应 GPU 后端，CPU 推理': ('no GPU backend in runtime, CPU mode', 'ランタイムにGPUバックエンドなし、CPU推論', '런타임 GPU 백엔드 없음, CPU 추론', 'нет GPU-бэкенда в рантайме, режим CPU'),
            '核显，CPU 推理': ('integrated GPU, CPU mode', '内蔵GPU、CPU推論', '내장 GPU, CPU 추론', 'встроенный GPU, режим CPU'),
            'Metal 统一内存': ('Metal unified memory', 'Metalユニファイドメモリ', 'Metal 통합 메모리', 'единая память Metal'),
        }.items():
            result = result.replace(source, translations[index])
    result = result.replace(' 个文本块', (' blocks', ' ブロック', '개 블록', ' блоков')[index])
    result = re.sub(r'(\d+)分(\d+)秒', lambda m: f'{m[1]}m {m[2]}s', result)
    result = re.sub(r'(\d+)秒', lambda m: f'{m[1]}s', result)
    return result

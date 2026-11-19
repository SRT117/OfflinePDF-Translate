<p align="center">
  <img src="../assets/icon.png" width="96" height="96" alt="OfflinePDF Translate">
</p>

<h1 align="center">OfflinePDF-Translate</h1>

<p align="center">
  완전 로컬 오프라인 학술 PDF 번역 워크벤치 · 이중 언어 대조 · 번역문 출력 · 하드웨어 적응형 추론
</p>

<p align="center">
  <a href="#설치-및-사용법">설치 및 사용법</a>
  · <a href="#데이터-및-개인정보">데이터 및 개인정보</a>
  · <a href="#시스템-호환성">시스템 호환성</a>
  · <a href="#자주-묻는-질문">자주 묻는 질문</a>
  · <a href="BUILD.md">빌드 안내</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Windows-10%2F11%20x64-0078D6?style=flat-square" alt="Windows 10/11 x64">
  <img src="https://img.shields.io/badge/Python-3.13%20Bundled-3776AB?style=flat-square" alt="Python 3.13">
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg?style=flat-square" alt="AGPL-3.0 License"></a>
</p>

<p align="center">
  <a href="../README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | <a href="README.ja.md">日本語</a> | 한국어 | <a href="README.ru.md">Русский</a>
</p>

---

OfflinePDF Translate은 로컬 오프라인 환경에서의 PDF 문헌 번역에 특화된 프로젝트입니다. 설치 프로그램을 다운로드하면 인터넷에 연결되지 않은 오프라인 컴퓨터에서도 바로 설치해 실행할 수 있으며, PDF 문헌과 일상적인 번역 작업을 무료로 무제한 수행할 수 있습니다. 설치 프로그램을 USB 메모리에 저장해 오프라인 컴퓨터에 꽂아 즉시 설치 및 실행하는 것도 가능합니다. Tencent Hunyuan MT2-1.8B 모델을 기반으로 38개 언어 및 방언 간의 상호 번역을 지원합니다.

이 프로젝트는 BabelDOC과 DocLayout-YOLO를 기반으로 PDF 파싱, 레이아웃 인식 및 이중 언어 조판(텍스트 추출이 가능한 PDF만 지원하며 스캔 이미지 PDF는 미지원)을 수행하고, llama.cpp와 Tencent Hunyuan(Hy-MT2-1.8B) 모델로 로컬 추론 번역을 처리합니다. 독립적인 Python 런타임, 추론 엔진, 양자화 모델 및 글꼴/레이아웃 자산을 내장하고 있으며, 일괄 작업 큐, 이중 언어 대조 및 번역문 전용 PDF 출력, 문서 단위 프로세스 격리, 자동 하드웨어 스케줄링(CPU 전용 환경에서도 안정 실행)을 지원합니다.

## 설치 및 사용법

### Windows 원클릭 설치

1. GitHub Releases에서 최신 버전의 `OfflinePDF-Translate-Setup.exe`를 다운로드합니다.
2. 더블 클릭하여 설치합니다. 상용 코드 서명 인증서를 구매하지 않은 오픈소스 프로젝트이므로 Windows SmartScreen 경고가 나타나면 **“추가 정보” → “실행”**을 클릭하여 계속 진행하세요.
3. 프로그램을 실행하고 PDF 파일을 끌어다 놓은 뒤 하단에서 대상 언어를 선택하고 시작을 클릭합니다.

### 출력 및 품질 경계

- 원본 디렉터리에 `[원본명].翻译.pdf`와 `[원본명].对照.pdf`가 생성되며, 두 PDF 모두 유효성 검사를 통과해야 성공으로 표시됩니다. 원본 디렉터리에 쓰기 권한이 없으면 사용자 데이터 디렉터리의 `Translated PDFs`에 자동 저장됩니다.
- 재번역 성공 시 동일 디렉터리의 임시 파일을 통해 같은 이름의 출력을 원자적으로 교체하며, 실패 시 기존 출력이 보존됩니다.
- 자리 표시자 누락, 출력 잘림, 빈 번역문 또는 원문 그대로 반환이 의심되면 자동으로 재시도합니다. 재시도 후에도 문제가 남으면 조판 유지를 위해 자리 표시자를 복구하거나 원문을 유지하므로, "완료" 상태가 모든 문단의 완벽한 번역을 보장하지는 않습니다. 중요한 문헌은 실시간 스트림 및 번역문과 대조하여 확인하시기 바랍니다.
- **텍스트 추출 가능 PDF만 지원**: 비트맵 스캔본에는 내장 OCR 엔진이 없으므로 명확히 차단되며 먼저 OCR을 수행하도록 안내합니다.
- **복잡한 문자 조판**: UI는 내장 글꼴을 사용하고 PDF 조판은 오프라인 글꼴을 임베드합니다. 아랍어 등 오른쪽에서 왼쪽으로 쓰는 문자와 일부 복잡한 조합 문자는 현재 조판이 부정확할 수 있으며, 해당 대상 언어를 선택하면 UI에 경고가 표시됩니다.

### 인터페이스 언어 / Interface language

창 오른쪽 상단에서 **简体中文, English, 日本語, 한국어, Русский**를 전환할 수 있으며 즉시 적용되고 자동 저장됩니다. 창 하단의 "대상 언어"는 PDF 번역 언어를 독립적으로 제어합니다.

### 일상 번역 워크벤치

상단 바의 **일상 번역** 탭으로 전환하면 PDF 작업과 충돌하지 않는 로컬 즉시 텍스트 번역을 사용할 수 있습니다(PDF 번역 중에는 자동으로 상호 배타 처리됨):

- **자동 상호 번역 및 38개 언어**: `중 ⇄ 영` 자동 감지 상호 번역 및 지원되는 38개 언어·방언 간 스트리밍 번역(`Ctrl+Enter` 또는 자동 번역)을 지원합니다.
- **PDF 줄바꿈 정리 및 파일 드롭**: PDF에서 복사한 텍스트의 하이픈 단어 분리와 불필요한 줄바꿈을 한 번에 정리(코드 블록 및 목록 구조 유지)하며 `.txt`, `.md`, `.srt`, `.vtt`, `.csv`, `.tsv`, `.log` 파일 드래그 앤 드롭을 지원합니다(1회 최대 5,000자).
- **Hy-MT2 스타일 및 용어집**: 스타일 프리셋(기본, 학술, 공식, 구어, 간결), 적중 용어만 주입하는 용어집, 선택 영역 빠른 용어 추가 및 로컬 기록을 제공합니다.

### 명령줄 도구(CLI)

소스 또는 내장 런타임 환경에서 CLI로 실행할 수 있습니다:

```powershell
.\runtime\python\python.exe cli.py translate paper.pdf --lang-in auto --lang-out ko
```

`-o/--output`, `-p/--pages`(예: `1-3`, `1,3,5`), `--threads`, `--parallel`, `--ctx-size`, `--gpu-layers` / `--ngl`, `--model`, `--runtime`, `--no-background` 옵션을 지원합니다.

---

## 데이터 및 개인정보

- **완전 로컬 오프라인 실행**: 계정 체계, 원격 측정(텔레메트리), 광고가 없으며 클라우드 번역 API 키에 의존하지 않습니다.
- **루프백 네트워크 제한**: 실행 전 글꼴, CMap, 토크나이저, ONNX 및 GGUF 준비 상태를 점검합니다. 번역 하위 프로세스의 Python 네트워크 연결은 루프백 주소로 제한되며, 로컬 `llama-server`는 `127.0.0.1`에서만 수신 대기합니다(참고: 이는 애플리케이션 계층의 정책 제한이며 OS 수준의 샌드박스 격리가 아닙니다).
- **데이터 저장 위치**: 캐시, 작업 큐 및 실행 로그는 사용자 데이터 디렉터리(Windows의 경우 `%LOCALAPPDATA%\OfflinePDF Translate`)에 저장되므로 설치 디렉터리에 쓰기 권한이 필요하지 않습니다. 구버전 데이터 디렉터리는 계속 유지되며, 제거 시 실행 중 생성된 캐시와 로그는 자동 삭제되지 않습니다. 문서 중간 파일은 독립된 임시 디렉터리에 저장되었다가 종료 시 정리됩니다.

---

## 시스템 호환성

- **Windows 10/11(64비트, x64)**: 공식 완전 지원. 모든 런타임을 내장한 단일 파일 설치 프로그램을 제공하며 설치 단계에서 최소 OS 버전을 확인합니다.
- **Windows 7/8**: **미지원**(내장된 Python 3.13 및 PySide6 6.11이 구버전 Windows 지원을 종료했습니다).
- **macOS / Linux**: 공식 서명된 설치 프로그램은 제공하지 않습니다. 핵심 코드에는 기본 플랫폼 대응 및 소스 테스트 호환성만 유지되며, Python 3.13 환경과 해당 플랫폼용 `llama-server`를 직접 준비해야 합니다.

---

## 스케줄링 및 하드웨어 정책

- 문서는 큐 순서대로 처리되며, 단일 문서 내부에서는 사용 가능한 메모리와 장치에 따라 산출된 추론 슬롯으로 병렬 처리됩니다. 각 문서는 독립된 격리 프로세스에서 실행됩니다.
- 기본 단락 추론 제한 시간은 600초(10분), 무진행 제한 시간은 900초이며, 저전력 노트북에서 CPU만으로 밤새 실행하거나 시스템 절전 방지 깨우기 잠금을 지원합니다. Windows에서는 Job Object를 통해 조판 보조 하위 프로세스를 일괄 회수합니다.
- 스레드 수, 동시 슬롯, 컨텍스트 창 및 GPU 오프로드 레이어 수는 모두 자동 감지 및 UI 수동 미세 조정을 지원합니다.

---

## 자주 묻는 질문

### 외장 그래픽카드 없이 CPU만으로도 사용할 수 있나요?
네. 설치 프로그램에 전체 CPU 명령어 집합 백엔드가 기본 내장되어 있어 현재 CPU에 맞게 적절한 스레드 수와 컨텍스트 창을 자동으로 산출합니다. 다만 긴 문헌은 번역 시간이 더 길어집니다.

### 설치 프로그램 실행 시 "Windows의 PC 보호" 창이 뜨면 어떻게 하나요?
상용 코드 서명 인증서를 구매하지 않은 개인 오픈소스 프로젝트에서 발생하는 일반적인 SmartScreen 안내입니다. **“추가 정보” → “실행”**을 클릭하면 정상적으로 설치됩니다.

### 일부 PDF에서 번역 불가 또는 스캔본으로 인식되는 이유는 무엇인가요?
이 프로젝트는 PDF 내부에 포함된 추출 가능한 텍스트와 레이아웃 객체에 의존합니다. 순수 이미지 스캔본은 텍스트 레이어가 없으므로 먼저 OCR 도구를 사용해 이중 레이어 PDF로 변환한 뒤 가져와야 합니다.

### 번역된 파일은 어디에 저장되나요?
기본적으로 원본 PDF가 있는 디렉터리(`[원본명].翻译.pdf` 및 `[원본명].对照.pdf`)에 직접 출력됩니다. 원본 디렉터리가 쓰기 보호되어 있으면 `%LOCALAPPDATA%\OfflinePDF Translate\Translated PDFs`에 자동 저장됩니다.

### 실행 실패나 번역 오류는 어떻게 확인하나요?
`%LOCALAPPDATA%\OfflinePDF Translate\logs` 아래의 실행 로그와 `llama-server.log`를 확인할 수 있습니다. Issue를 제출할 때는 민감 정보를 제거한 오류 로그와 시스템 구성을 첨부해 주시고, 개인정보가 포함된 논문 원문은 절대 업로드하지 마세요.

---

## 빌드 및 개발

소스 저장소에는 내장 Python 런타임, `llama.cpp` 바이너리, GGUF 모델 및 대부분의 글꼴/ONNX 자산이 포함되어 있지 않습니다. 로컬 빌드 및 패키징 절차는 [빌드 안내(docs/BUILD.md)](BUILD.md)를 참고하세요.

6개의 직접 의존성(`babeldoc`, `pymupdf`, `psutil`, `bitstring`, `tibs`, `PySide6-Essentials`)은 여러 전이 의존성(예: 레이아웃 처리용 OpenCV 또는 BabelDOC 내부에서 참조하는 `openai` SDK)을 가져오지만, 이러한 의존성이 존재한다고 해서 클라우드 번역이나 OCR이 활성화되는 것은 아닙니다.

---

## 감사의 글 및 관련 프로젝트

OfflinePDF Translate은 다음의 훌륭한 오픈소스 프로젝트와 모델을 기반으로 구현되었으며 영감을 받았습니다:

- [BabelDOC](https://github.com/funstory-ai/BabelDOC)(AGPL-3.0): 핵심 PDF 파싱, 레이아웃 복원, 수식 보호 및 이중 언어 조판 엔진.
- [PDFMathTranslate / pdf2zh](https://github.com/PDFMathTranslate/PDFMathTranslate): 학술 문헌 이중 언어 대조 번역 워크플로의 선구적 프로젝트.
- [llama.cpp](https://github.com/ggml-org/llama.cpp)(MIT): 가볍고 고성능인 로컬 GGUF 모델 추론 런타임.
- [Tencent-Hunyuan/Hy-MT2](https://github.com/Tencent-Hunyuan/Hy-MT2) / [Hy-MT2-1.8B](https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF): 온디바이스 다국어 번역 역량 및 공식 이중 트랙 지시문, 용어 개입, 스타일 제어 템플릿 규격 제공.
- [DocLayout-YOLO](https://github.com/opendatalab/DocLayout-YOLO)(AGPL-3.0): 논문 레이아웃 탐지 모델.
- [KFERMercer/RiipL](https://github.com/KFERMercer/RiipL): Qt 6 및 Hy-MT2 기반 데스크톱 번역기. 일상 번역 모듈의 용어집 관리, 스타일 프리셋 및 클립보드 워크플로에 영감을 줌.
- [ziniee/local-translate](https://github.com/ziniee/local-translate): llama.cpp가 내장된 로컬 데스크톱 번역기. 코드 블록 원문 통과, 장문 슬라이딩 윈도우 분할 및 적중 용어만 주입하는 구현 아이디어에 영감을 줌.
- [CopyTranslator](https://github.com/CopyTranslator/CopyTranslator) & [msc306/AI-Translate](https://github.com/msc306/AI-Translate): PDF 복사 줄바꿈 정리, 클립보드 감시, 스트리밍 속도 표시 및 자동 복사 등 일상 고빈도 UX 설계에 영감을 줌.
- [PySide6 (Qt for Python)](https://doc.qt.io/qtforpython-6/) & [PyMuPDF](https://github.com/pymupdf/PyMuPDF): 데스크톱 UI 프레임워크 및 저수준 PDF 처리 지원.

## 참여 및 라이선스

- 버그 제보 및 기능 제안: [GitHub Issues](../../../issues)
- 기여 안내: [CONTRIBUTING.md](../CONTRIBUTING.md) · 행동 강령: [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md)
- 보안 정책: [SECURITY.md](../SECURITY.md) · 제3자 고지: [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)

본 프로젝트의 소스코드는 [GNU AGPL-3.0](../LICENSE)으로 배포되며, 서드파티 구성 요소, 글꼴 및 모델 가중치는 각자의 원래 라이선스를 따릅니다.

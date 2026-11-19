# Contributing

Thank you for helping improve OfflinePDF Translate. Issues and pull requests are welcome; please keep changes focused and describe the problem, the solution and the checks performed. English or Chinese reports are welcome.

## Development

The current supported application runtime is Windows x64 and Python 3.13. Follow [the build guide](docs/BUILD.md) to prepare the runtime and assets, then run:

```powershell
.\runtime\python\python.exe -m unittest discover -s tests
.\runtime\python\python.exe gui.py --check-runtime
```

For UI changes, mention the Windows version, interface language and steps tested. Translation and document processing changes should include a small redistributable example or a synthetic fixture; do not upload private papers, user data, logs containing document text, API keys, generated models or build artifacts. For new dependencies, fonts, models or copied code, document the exact source, version and license in the third-party notices. Contributions are accepted under the project license; retain third-party copyright and license notices.

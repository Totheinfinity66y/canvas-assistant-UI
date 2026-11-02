"""Portable entry point; no hard-coded computer paths or automatic installation."""
from pathlib import Path
import importlib.util
import sys


def main():
    if sys.version_info < (3, 11):
        print('需要 Python 3.11 或更高版本。请参阅 GETTING_STARTED.md。')
        return 2
    if importlib.util.find_spec('requests') is None:
        print('当前 Python 缺少 requests。请在自己的环境安装 requirements.txt 中的依赖。')
        print('Python executable:', sys.executable)
        print('配置向导说明：GETTING_STARTED.md；本程序不会自动安装。')
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parent/'scripts'))
    from canvas_manager import main as manager_main
    return manager_main()


if __name__ == '__main__':
    sys.exit(main())

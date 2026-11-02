"""Build a credential-free, allowlisted portable zip. Never glob the repository."""
import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    'CANVAS_UI_README.md':'README.md',
    'run_canvas.py':'run_canvas.py', 'start_canvas.cmd':'start_canvas.cmd',
    'requirements-canvas.txt':'requirements.txt', 'GETTING_STARTED.md':'GETTING_STARTED.md',
    'canvas-manager.example.json':'canvas-manager.example.json', 'LICENSE':'LICENSE',
    'scripts/canvas_manager.py':'scripts/canvas_manager.py',
    'scripts/canvas_settings.py':'scripts/canvas_settings.py',
    'scripts/canvas_manager.html':'scripts/canvas_manager.html',
    'scripts/canvas_assignments.html':'scripts/canvas_assignments.html',
    'scripts/canvas_setup.html':'scripts/canvas_setup.html',
    'scripts/assets/canvas-token-settings.png':'scripts/assets/canvas-token-settings.png',
    'scripts/build_canvas_package.py':'scripts/build_canvas_package.py',
    'tests/test_canvas_manager.py':'tests/test_canvas_manager.py',
    'tests/test_canvas_manager_v2.py':'tests/test_canvas_manager_v2.py',
    'tests/test_canvas_frontend.cjs':'tests/test_canvas_frontend.cjs',
}


def build(output, root=ROOT):
    output, root = Path(output), Path(root)
    sources = {name:root/name if (root/name).is_file() else root/target for name,target in FILES.items()}
    for name, path in sources.items():
        if not path.is_file():
            raise FileNotFoundError(name)
    output.parent.mkdir(parents=True, exist_ok=True)
    # x mode prevents replacing an existing release; no personal configuration is included.
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for source, target in FILES.items():
            archive.write(sources[source], 'canvas-manager/'+target)
    return output


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'dist/canvas-assistant-ui-v2.1.0.zip')
    args=parser.parse_args()
    print(build(args.output))

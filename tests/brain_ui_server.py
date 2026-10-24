"""Synthetic provider for browser QA; never contacts Cloud.ru."""

from pathlib import Path
import tempfile

import test_brain as tests


with tempfile.TemporaryDirectory() as folder:
    cloud = tests.FakeCloud()
    with tests.launch.server_module.LocalServer(("127.0.0.1", 0), tests.launch.server_module.Handler) as server:
        server.brain_service = tests.brain.BrainService(Path(folder), cloud.factory, start_worker=True)
        print(f"http://127.0.0.1:{server.server_port}/brain", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

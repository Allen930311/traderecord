import json
import subprocess, sys
from pathlib import Path

def test_validate_cli_rejects_invalid_record(tmp_path: Path):
    path=tmp_path/"invalid.json"; path.write_text(json.dumps({"schema_version":"bad"}),encoding="utf-8")
    result=subprocess.run([sys.executable,"-m","trading_os","validate",str(path)],capture_output=True,text=True)
    assert result.returncode==1

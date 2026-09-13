"""Adapter-owned, atomic host continuation state."""
from __future__ import annotations
import json, os, tempfile
from pathlib import Path
from typing import Any

class BridgeState:
    def __init__(self,path:Path):
        self.path=Path(path); self.data={"threads":{},"pending":{}}
        if self.path.exists():
            try:
                loaded=json.loads(self.path.read_text());
                if isinstance(loaded,dict): self.data.update(loaded)
            except (OSError,ValueError): pass
    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(prefix=".bridge-",dir=self.path.parent)
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,"w") as f: json.dump(self.data,f,sort_keys=True); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,self.path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

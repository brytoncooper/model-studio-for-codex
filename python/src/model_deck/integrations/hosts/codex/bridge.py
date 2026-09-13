"""Loopback OpenAI Responses facade backed solely by the public engine RPC API."""
from __future__ import annotations
import json, os, secrets, socket, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from .input_normalization import normalize_codex_input
from .tool_conversion import convert_tools, restore_function_call, ToolConversionError
from .state import BridgeState

class EngineRPC:
    def __init__(self, rendezvous:Path, credential:Path):
        self.rendezvous=rendezvous; self.credential=credential; self._id=0
    def call(self, method:str, params:dict[str,Any])->Any:
        desc=json.loads(self.rendezvous.read_text()); token=self.credential.read_text().strip()
        self._id+=1; frame={"jsonrpc":"2.0","id":self._id,"method":method,"params":params}
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
            s.settimeout(15); s.connect(desc["socket_path"]); s.sendall((json.dumps(frame)+"\n").encode())
            line=b""
            while not line.endswith(b"\n"): line+=s.recv(65536)
        result=json.loads(line); 
        if "error" in result: raise RuntimeError(result["error"])
        return result.get("result")

class CodexResponsesBridge:
    def __init__(self, *, rendezvous_path, credential_path, profile, state_path, token_path, descriptor_path):
        self.engine=EngineRPC(Path(rendezvous_path),Path(credential_path)); self.profile=profile
        self.state=BridgeState(Path(state_path)); self.token_path=Path(token_path); self.descriptor_path=Path(descriptor_path)
        self.token=secrets.token_urlsafe(32); self._server=None
    def _provision(self):
        p=self.profile
        connections=self.engine.call("engine.v1.connections.list",{}).get("connections",[])
        if not any(x.get("connection_id")==p.connection_id for x in connections):
            self.engine.call("engine.v1.connections.save",{"connection_id":p.connection_id,"provider_id":p.provider_id,"endpoint_config_ref":p.endpoint_config_ref,"credential_ref":p.credential_ref})
        models=self.engine.call("engine.v1.models.list",{}).get("models",[])
        if not any(x.get("model_id")==p.provider_model_id for x in models):
            self.engine.call("engine.v1.models.register",{"model_id":p.provider_model_id,"provider_id":p.provider_id,"display_name":p.display_name,"connection_id":p.connection_id,"capability_snapshot_ref":p.capability_snapshot_ref})
    def start(self):
        self._provision(); self.token_path.parent.mkdir(parents=True,exist_ok=True); self.token_path.write_text(self.token); os.chmod(self.token_path,0o600)
        class Handler(BaseHTTPRequestHandler):
            def do_POST(h): self._handle(h)
            def do_GET(h): h.send_error(405)
            def do_PUT(h): h.send_error(405)
            def do_DELETE(h): h.send_error(405)
            def log_message(h,*a): pass
        self._server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        host,port=self._server.server_address
        self.descriptor_path.parent.mkdir(parents=True,exist_ok=True)
        payload={"schema_version":1,"base_url":f"http://127.0.0.1:{port}/v1","provider_id":self.profile.provider_id,"model":self.profile.provider_model_id,"billing_description":self.profile.billing_description,"token_path":str(self.token_path)}
        tmp=self.descriptor_path.with_suffix(".tmp"); tmp.write_text(json.dumps(payload)); os.chmod(tmp,0o600); os.replace(tmp,self.descriptor_path)
        threading.Thread(target=self._server.serve_forever,daemon=True).start(); return payload["base_url"]
    def stop(self):
        if self._server: self._server.shutdown(); self._server.server_close()
    def _handle(self,h):
        if h.path != "/v1/responses": h.send_error(404); return
        if h.headers.get("Authorization") != f"Bearer {self.token}": h.send_error(401); return
        try:
            body=json.loads(h.rfile.read(int(h.headers.get("Content-Length","0"))))
            tools,aliases=convert_tools(body.get("tools")); meta=json.loads(h.headers.get("x-codex-turn-metadata","{}")); thread=meta.get("thread_id")
            if not isinstance(thread,str) or not thread: raise ValueError("thread_id required")
            normalized=normalize_codex_input(body.get("input",[]),aliases)
            self.state.data.setdefault("threads",{}); sid=self.state.data["threads"].get(thread)
            if not sid:
                sid=self.engine.call("engine.v1.sessions.create",{})["session_id"]; self.state.data["threads"][thread]=sid; self.state.save()
            pending=body.get("input",[])
            match=next((x for x in pending if isinstance(x,dict) and x.get("type")=="function_call_output" and x.get("call_id") in self.state.data["pending"]),None)
            if match:
                run_id=self.state.data["pending"][match["call_id"]]["run_id"]; self.engine.call("engine.v1.runs.submit_tool_result",{"run_id":run_id,"call_id":match["call_id"],"output":match.get("output")})
            else:
                run=self.engine.call("engine.v1.runs.start",{"session_id":sid,"model_id":self.profile.provider_model_id,"input":normalized,"tools":tools,"options":{"parallel_tool_calls":False}}); run_id=run["run_id"]
            h.send_response(200); h.send_header("Content-Type","text/event-stream"); h.end_headers(); h.wfile.write(b"data: {\"type\":\"response.completed\",\"run_id\":\""+run_id.encode()+b"\"}\n\n"); h.wfile.flush()
        except (ValueError,TypeError,KeyError,ToolConversionError) as exc: h.send_error(400,str(exc))
        except (BrokenPipeError,ConnectionError): pass

__all__=["CodexResponsesBridge","EngineRPC"]

"""Optional end-to-end check with an installed Codex and a localhost model stub.

No real model is called. Global/user agent settings are not modified.
Run: python tests/integration_codex.py
"""

import os, json, threading, subprocess, tempfile, shutil, signal, time, re, sys, shlex
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
ROOT = Path(__file__).resolve().parents[1]
root = Path(tempfile.mkdtemp(prefix="memguard-codex-integration-"))
state = {}
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(404); self.end_headers()
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))))
        state["requests"] = state.get("requests",0)+1
        outputs=[x.get("output","") for x in data.get("input",[]) if isinstance(x,dict) and x.get("type")=="function_call_output"]
        state["tool_outputs"]=outputs
        latest=outputs[-1] if outputs and isinstance(outputs[-1],str) else ""
        running=re.search(r"Process running with session ID (\d+)",latest)
        tools = data.get("tools",[])
        names = []
        namespace = None
        for tool in tools:
            names.append(tool.get("name",tool.get("type","?")))
            if tool.get("type") == "namespace":
                for sub in tool.get("tools",[]):
                    names.append(tool.get("name","")+"."+sub.get("name","?"))
                    if sub.get("name")=="exec_command": namespace=tool.get("name")
        state["tools"] = names
        self.send_response(200)
        self.send_header("Content-Type","text/event-stream")
        self.send_header("Connection","close")
        self.end_headers()
        response = {"id":"resp_probe_"+str(state["requests"]),"object":"response","created_at":int(time.time()),"status":"in_progress","model":"gpt-6.1-sol","output":[]}
        events=[("response.created",{"response":dict(response)})]
        if state["requests"]==1 or running:
            args={"cmd":state["command"],"login":False,"max_output_tokens":2000,"yield_time_ms":1000}
            tool_name="exec_command"
            if running:
                tool_name="write_stdin"
                args={"session_id":int(running[1]),"chars":"","max_output_tokens":2000,"yield_time_ms":1000}
            item={"type":"function_call","id":"fc_probe_"+str(state["requests"]),"call_id":"call_probe_"+str(state["requests"]),"name":tool_name,"arguments":json.dumps(args),"status":"completed"}
            if namespace:item["namespace"]=namespace
            pending=dict(item,arguments="",status="in_progress")
            events += [("response.output_item.added",{"output_index":0,"item":pending}),
                       ("response.function_call_arguments.delta",{"item_id":item["id"],"output_index":0,"delta":item["arguments"]}),
                       ("response.function_call_arguments.done",{"item_id":item["id"],"output_index":0,"arguments":item["arguments"]}),
                       ("response.output_item.done",{"output_index":0,"item":item})]
        else:
            item={"type":"message","id":"msg_probe","role":"assistant","status":"completed","content":[{"type":"output_text","text":"probe complete","annotations":[]}]}
            events += [("response.output_item.added",{"output_index":0,"item":dict(item,status="in_progress",content=[])}),
                       ("response.content_part.added",{"item_id":"msg_probe","output_index":0,"content_index":0,"part":{"type":"output_text","text":"","annotations":[]}}),
                       ("response.output_text.delta",{"item_id":"msg_probe","output_index":0,"content_index":0,"delta":"probe complete"}),
                       ("response.output_text.done",{"item_id":"msg_probe","output_index":0,"content_index":0,"text":"probe complete"}),
                       ("response.output_item.done",{"output_index":0,"item":item})]
        response.update(status="completed",output=[item],usage={"input_tokens":10,"output_tokens":10,"total_tokens":20,"input_tokens_details":{"cached_tokens":0},"output_tokens_details":{"reasoning_tokens":0}})
        events.append(("response.completed",{"response":response}))
        try:
            for i,(kind,body) in enumerate(events):
                body.update(type=kind,sequence_number=i)
                self.wfile.write(("event: "+kind+"\ndata: "+json.dumps(body)+"\n\n").encode())
                self.wfile.flush()
        except (BrokenPipeError,ConnectionResetError):pass
        self.close_connection=True
server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    mode = os.environ.get("MEMGUARD_MODE", "container")
    for label in ("success", "large_healthy", "environment_pressure"):
        case = root / label
        case.mkdir()
        (case / "home").mkdir()
        state.clear()
        state.update(requests=0)
        cgroup = case / "cgroup"
        cgroup.mkdir()
        (cgroup / "memory.max").write_text("max" if mode == "wsl" else str(1024**3))
        (cgroup / "memory.current").write_text(str(100 * 1024**2))
        (cgroup / "memory.stat").write_text("inactive_file 0\n")
        meminfo = case / "meminfo"
        meminfo.write_text("MemTotal: 1048576 kB\nMemAvailable: 921600 kB\nSwapTotal: 2097152 kB\nSwapFree: 2097152 kB\n")
        if label == "success":
            state["command"] = "printf MEMGUARD_SUCCESS; printf MEMGUARD_STDERR >&2; exit 7"
        elif label == "large_healthy":
            state["command"] = shlex.join([sys.executable, "-c", "import time; data=bytearray(48*1024*1024); time.sleep(.2); print('MEMGUARD_GROWTH_OK')"])
        else:
            metrics = meminfo if mode == "wsl" else cgroup / "memory.current"
            pressured = ("MemTotal: 1048576 kB\nMemAvailable: 65536 kB\nSwapTotal: 2097152 kB\nSwapFree: 2097152 kB\n"
                         if mode == "wsl" else str(940 * 1024**2))
            code = (f"import time,pathlib; temporary=pathlib.Path({str(metrics)+'.new'!r}); "
                    f"temporary.write_text({pressured!r}); temporary.replace({str(metrics)!r}); time.sleep(10)")
            state["command"] = shlex.join([sys.executable, "-c", code])
        (case/"home"/"config.toml").write_text('model = "gpt-6.1-sol"\nmodel_provider = "probe"\n[features]\nshell_snapshot = false\nunified_exec = true\n[model_providers.probe]\nname = "Local probe"\nbase_url = "http://127.0.0.1:'+str(server.server_port)+'/v1"\nwire_api = "responses"\nrequires_openai_auth = false\n')

        hook_path=str(ROOT/"bin"/"memguard-codex-hook")
        (case/"home"/"hooks.json").write_text(json.dumps({"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":hook_path,"timeout":5}]}]}}))

        env=os.environ.copy();env.update(CODEX_HOME=str(case/"home"),MEMGUARD_MODE=mode,MEMGUARD_LIMIT="1M",
                                        MEMGUARD_INTERVAL="0.05",MEMGUARD_STATE_DIR=str(case/"state"),
                                        MEMGUARD_CGROUP_DIR=str(cgroup),MEMGUARD_MEMINFO_FILE=str(meminfo))
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENAI_BASE_URL", None)
        env.pop("BASH_ENV", None)
        out=case/"stdout";err=case/"stderr"
        with out.open("wb") as stdout,err.open("wb") as stderr:
            process=subprocess.Popen(["codex","--dangerously-bypass-hook-trust","exec","--json","--skip-git-repo-check","--sandbox","danger-full-access","-C",str(case),"Run the shell probe."],env=env,stdout=stdout,stderr=stderr,start_new_session=True)
            deadline=time.monotonic()+55
            reason=None
            while process.poll() is None:
                if time.monotonic()>deadline:reason="timeout";break
                try:
                    status=Path("/proc")/str(process.pid)/"status"
                    match=re.search(r"VmRSS:\s+(\d+)",status.read_text())
                    if match and int(match.group(1))>400*1024:reason="RSS limit";break
                except OSError:pass
                time.sleep(.1)
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.wait()
        result=out.read_text(errors="replace");errors=err.read_text(errors="replace")
        outputs="\n".join(x for x in state.get("tool_outputs",[]) if isinstance(x,str))
        if label == "success":
            passed = "code 7" in outputs and "MEMGUARD_SUCCESS" in outputs and "MEMGUARD_STDERR" in outputs
        elif label == "large_healthy":
            passed = "code 0" in outputs and "MEMGUARD_GROWTH_OK" in outputs
        else:
            passed = "code 125" in outputs and "environment_pressure" in outputs
        print(json.dumps({"case":label,"passed":passed,"requests":state.get("requests"),"tool_outputs":state.get("tool_outputs"),"stderr_tail":errors[-500:]},ensure_ascii=False),flush=True)
        if not passed:raise RuntimeError("Codex integration failed: "+label)
finally:
    server.shutdown();server.server_close();shutil.rmtree(root)

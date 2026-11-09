"""A stand-in MCP Apps host, for trying the in-chat views in a plain browser (developers and the review panel).

    uv run python scripts/view_host.py [--port 8790] [--data DIR] [--sample]

It starts the real Talent Search MCP server over stdio (`talent mcp`), says it shows MCP Apps views, and serves a
page at http://127.0.0.1:8790 where you pick a tool, give its arguments and press Run. The tool's view is loaded
into a sandboxed iframe the way Claude does it, and the page plays the host's side of the protocol:
ui/initialize (light or dark, inline or fullscreen), the tool result, tools/call back to the server, and a log of
everything the view asks for (open a link, send a message, update the model context, change display mode).

Nothing here ships in the app. Claude's real frame is about 720 px wide inline; the page uses the same.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

UI = "io.modelcontextprotocol/ui"
MIME = "text/html;profile=mcp-app"


class AppsClientSession(ClientSession):
    """A client that tells the server it shows MCP Apps views (the one thing the stock client can't say)."""

    async def initialize(self) -> types.InitializeResult:  # type: ignore[override]
        result = await self.send_request(
            types.ClientRequest(types.InitializeRequest(params=types.InitializeRequestParams(
                protocolVersion=types.LATEST_PROTOCOL_VERSION,
                capabilities=types.ClientCapabilities(experimental={UI: {"mimeTypes": [MIME]}}),
                clientInfo=types.Implementation(name="talent-view-host", version="1")))),
            types.InitializeResult)
        await self.send_notification(types.ClientNotification(types.InitializedNotification()))
        return result


class Bridge:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.session: ClientSession | None = None
        self.stack = AsyncExitStack()
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        env = {**os.environ, "TALENT_DATA_DIR": self.data_dir}
        params = StdioServerParameters(command=sys.executable, args=["-m", "talent", "mcp"], env=env)
        read, write = await self.stack.enter_async_context(stdio_client(params))
        self.session = await self.stack.enter_async_context(AppsClientSession(read, write))
        await self.session.initialize()

    async def tools(self) -> list[dict[str, Any]]:
        res = await self.session.list_tools()
        return [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in res.tools]

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            res = await self.session.call_tool(name, args)
        return res.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def resource(self, uri: str) -> str:
        res = await self.session.read_resource(uri)
        return "".join(getattr(c, "text", "") for c in res.contents)


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>MCP view host</title><style>
body{margin:0;font:14px system-ui,sans-serif;display:grid;grid-template-columns:300px 1fr 320px;height:100vh;background:#f4f3ef;color:#1c1c1a}
body.dark{background:#262624;color:#ecebe6}
aside{padding:12px;overflow:auto;border-right:1px solid #ccc}#log{border-left:1px solid #ccc;border-right:0;font:12px ui-monospace,monospace;white-space:pre-wrap}
main{overflow:auto;padding:24px}#chat{max-width:720px;margin:0 auto}#chat.full{max-width:none;margin:0;position:fixed;inset:0;left:300px;right:320px;background:inherit;padding:16px;z-index:5}
iframe{width:100%;border:0;display:block;background:transparent}textarea,select,input{width:100%;font:12px ui-monospace,monospace;box-sizing:border-box}
textarea{height:160px}button{margin-top:6px}.bubble{background:#fff;border-radius:10px;padding:8px 12px;margin-bottom:10px;font-size:13px}body.dark .bubble{background:#30302e}
.fullbar{display:none;justify-content:space-between;margin-bottom:8px}#chat.full .fullbar{display:flex}
</style></head><body>
<aside><h3>Tool</h3><select id="tool"></select><p id="desc" style="font-size:11px;opacity:.7;max-height:90px;overflow:auto"></p>
<label>Arguments (JSON)<textarea id="args">{}</textarea></label>
<label><input type="checkbox" id="dark" style="width:auto"> Dark theme</label>
<label><input type="checkbox" id="nofull" style="width:auto"> Host has no fullscreen</label>
<label><input type="checkbox" id="nolinks" style="width:auto"> Host refuses links</label>
<button id="run">Run</button><p id="err" style="color:#b00"></p></aside>
<main><div id="chat"><div class="fullbar"><b id="fulltitle">Fullscreen</b><button id="close" aria-label="Close fullscreen">Close</button></div><div id="frames"></div></div></main>
<aside id="log"></aside>
<script>
const $=id=>document.getElementById(id);let TOOLS={};const log=(...a)=>{$("log").textContent=a.map(x=>typeof x==="string"?x:JSON.stringify(x)).join(" ")+"\n\n"+$("log").textContent};
window.HOSTLOG=[];
fetch("/api/tools").then(r=>r.json()).then(ts=>{for(const t of ts){TOOLS[t.name]=t;const ui=t._meta&&t._meta.ui;$("tool").append(new Option((ui&&ui.resourceUri?"◆ ":"")+t.name+(ui&&ui.visibility&&!ui.visibility.includes("model")?" (app only)":""),t.name))}$("tool").onchange()});
$("tool").onchange=()=>{const t=TOOLS[$("tool").value];$("desc").textContent=t?(t.description||""):""};
$("dark").onchange=()=>{document.body.classList.toggle("dark",$("dark").checked);for(const f of FRAMES)f.ctx({theme:$("dark").checked?"dark":"light"})};
let FRAMES=[];
function reset(){for(const r of FRAMES)window.removeEventListener("message",r.onmsg);FRAMES=[];$("frames").replaceChildren();$("chat").classList.remove("full")}
async function call(name,args){const r=await fetch("/api/call",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({name,arguments:args})});return r.json()}
$("run").onclick=async()=>{$("err").textContent="";let args;try{args=JSON.parse($("args").value||"{}")}catch(e){$("err").textContent="Arguments aren't JSON";return}
 reset();  // each Run replaces the frame: the last tool's view never stays on the page
 const name=$("tool").value,t=TOOLS[name];const res=await call(name,args);
 const text=(res.content||[]).map(c=>c.text||"").join("\n");const b=document.createElement("div");b.className="bubble";b.textContent=name+": "+text.slice(0,600)+(text.length>600?"…":"");$("frames").append(b);
 const uri=t&&t._meta&&t._meta.ui&&t._meta.ui.resourceUri;if(!uri){log("no view for",name);return}
 const html=await (await fetch("/api/resource?uri="+encodeURIComponent(uri))).text();mount(html,args,res,name)};
function mount(html,args,result,toolName){
 const f=document.createElement("iframe");f.setAttribute("sandbox","allow-scripts allow-forms");f.srcdoc=html;f.style.height="60px";f.title="view: "+toolName;$("frames").append(f);
 let mode="inline";const modes=()=>$("nofull").checked?["inline"]:["inline","fullscreen"];
 const send=m=>f.contentWindow.postMessage(m,"*");
 const ctx=p=>send({jsonrpc:"2.0",method:"ui/notifications/host-context-changed",params:p});
 const rec={f,ctx,toolName,onmsg:null};FRAMES.push(rec);
 function setMode(m){mode=m;if(m==="fullscreen"){$("chat").classList.add("full");f.style.height="calc(100vh - 80px)"}else{$("chat").classList.remove("full")}
  ctx({displayMode:mode,containerDimensions:m==="fullscreen"?{height:window.innerHeight-80,width:$("chat").clientWidth}:{maxHeight:6000,width:$("chat").clientWidth}})}
 $("close").onclick=()=>{if(mode==="fullscreen")setMode("inline")};
 rec.onmsg=async ev=>{if(ev.source!==f.contentWindow)return;const m=ev.data;if(!m||m.jsonrpc!=="2.0")return;
  window.HOSTLOG.push(m);if(m.method!=="ui/notifications/size-changed")log("view →",m);
  const ok=r=>send({jsonrpc:"2.0",id:m.id,result:r}),bad=(msg)=>send({jsonrpc:"2.0",id:m.id,error:{code:-32000,message:msg}});
  switch(m.method){
   case"ui/initialize":ok({protocolVersion:"2026-01-26",hostInfo:{name:"view-host",version:"1"},hostCapabilities:{openLinks:{},serverTools:{},serverResources:{},logging:{}},
     hostContext:{theme:$("dark").checked?"dark":"light",displayMode:"inline",availableDisplayModes:modes(),platform:"desktop",locale:"en-US",timeZone:Intl.DateTimeFormat().resolvedOptions().timeZone,
     containerDimensions:{maxHeight:6000,width:$("chat").clientWidth},toolInfo:{tool:TOOLS[toolName]}}});break;
   case"ui/notifications/initialized":send({jsonrpc:"2.0",method:"ui/notifications/tool-input",params:{arguments:args}});
     send({jsonrpc:"2.0",method:"ui/notifications/tool-result",params:result});break;
   case"ui/notifications/size-changed":if(mode==="inline")f.style.height=Math.min(m.params.height,6000)+"px";break;
   case"ui/open-link":if($("nolinks").checked)bad("Policy violation");else{log("OPEN LINK",m.params.url);ok({})}break;
   case"ui/message":{const b=document.createElement("div");b.className="bubble";b.style.background="#dfe8ff";b.textContent="You (from the view): "+JSON.stringify(m.params.content);$("frames").append(b);ok({});break}
   case"ui/update-model-context":log("MODEL CONTEXT",m.params);ok({});break;
   case"ui/request-display-mode":{const want=m.params&&m.params.mode;if(modes().includes(want))setMode(want);ok({mode});break}
   case"tools/call":{const t=TOOLS[m.params.name];const vis=t&&t._meta&&t._meta.ui&&t._meta.ui.visibility;
     if(!t){bad("Unknown tool");break}if(vis&&!vis.includes("app")){bad("This tool can't be called from a view");break}
     ok(await call(m.params.name,m.params.arguments||{}));break}
   case"resources/read":ok({contents:[{uri:m.params.uri,mimeType:MIME,text:await (await fetch("/api/resource?uri="+encodeURIComponent(m.params.uri))).text()}]});break;
   case"ping":ok({});break;
   default:if(m.id!=null)bad("Not supported by this host")}
 };
 window.addEventListener("message",rec.onmsg);
}
</script></body></html>"""


def build(bridge: Bridge) -> FastAPI:
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE

    @app.get("/api/tools")
    async def tools() -> JSONResponse:
        return JSONResponse(await bridge.tools())

    @app.post("/api/call")
    async def call(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(await bridge.call(body["name"], body.get("arguments") or {}))

    @app.get("/api/resource", response_class=HTMLResponse)
    async def resource(uri: str) -> str:
        return await bridge.resource(uri)

    return app


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--data", default=str(Path.cwd() / "view-host-data"), help="TALENT_DATA_DIR for the server")
    ap.add_argument("--sample", action="store_true", help="load the made-up sample candidates first")
    a = ap.parse_args()
    Path(a.data).mkdir(parents=True, exist_ok=True)
    if a.sample:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "talent", "sample-data",
                                                    env={**os.environ, "TALENT_DATA_DIR": a.data})
        await proc.wait()
    bridge = Bridge(a.data)
    await bridge.start()
    print(f"View host on http://127.0.0.1:{a.port} (data in {a.data})", flush=True)
    server = uvicorn.Server(uvicorn.Config(build(bridge), host="127.0.0.1", port=a.port, log_level="warning"))
    try:
        await server.serve()
    finally:
        await bridge.stack.aclose()


if __name__ == "__main__":
    asyncio.run(main())

import os
import sys
import re
import random
import string
import xml.etree.ElementTree as ET
import requests
import json
import argparse
import uuid
import threading
import subprocess
import csv
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

# 忽略 HTTPS 警告
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 核心数据类 ---

class ApiParameter:
    def __init__(self, name: str, tag_name: str, tag_position: str, param_type: str, required: bool, description: str = "", example: str = ""):
        self.name = name
        self.tag_name = tag_name
        self.tag_position = tag_position
        self.param_type = param_type
        self.required = required
        self.description = description
        self.example = example
        self.sub_parameters: Dict[str, 'ApiParameter'] = {}

class ApiDefinition:
    def __init__(self, name: str, version: str, protocols: List[str], methods: List[str], file_path: str):
        self.name = name
        self.version = version
        self.protocols = protocols
        self.methods = methods
        self.file_path = os.path.abspath(file_path)
        self.description = ""
        self.parameters: Dict[str, ApiParameter] = {}

class ScenarioStep:
    def __init__(self, case_id: str, module: str, item: str, steps: str, expected: str):
        self.case_id = case_id or f"C{uuid.uuid4().hex[:4]}"
        self.module = module
        self.item = item
        self.steps = steps
        self.expected = expected
        self.mapped_api: Optional[str] = None
        self.status: str = "待测试"

class BastionHostApiClient:
    def __init__(self, host: str, port: int, protocol: str = "http", base_path: str = "/"):
        self.host = host
        self.port = port
        self.protocol = protocol.lower()
        self.base_path = base_path
        self.apis: Dict[str, ApiDefinition] = {}
        self.gateway_paths: Dict[str, str] = {}

    def load_apis(self, pop_dir: str, gate_dir: str):
        for root, _, files in os.walk(pop_dir):
            for file in files:
                if file.endswith(".xml"): self._parse_pop_xml(os.path.join(root, file))
        for root, _, files in os.walk(gate_dir):
            for file in files:
                if file.endswith(".xml"): self._parse_gate_xml(os.path.join(root, file))

    def _parse_pop_xml(self, file_path: str):
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            if root.tag != "Api": return
            api_def = ApiDefinition(root.attrib.get("name"), root.attrib.get("version", ""), [], [], file_path)
            api_def.description = root.attrib.get("description", "")
            params_node = root.find("Parameters")
            if params_node is not None: self._parse_parameters(params_node, api_def.parameters)
            self.apis[api_def.name] = api_def
        except: pass

    def _parse_gate_xml(self, file_path: str):
        try:
            root = ET.parse(file_path).getroot()
            if root.tag.lower() != "api": return
            name = root.find("name").text
            for prop in root.findall(".//property"):
                if prop.attrib.get("mapping") == "path":
                    self.gateway_paths[name] = prop.attrib.get("value")
        except: pass

    def _parse_parameters(self, parent_node: ET.Element, target_dict: Dict[str, ApiParameter]):
        for p_node in parent_node.findall("Parameter"):
            name = p_node.attrib.get("name")
            req = p_node.attrib.get("required", "false").lower() in ["true", "ture"]
            param = ApiParameter(name, p_node.attrib.get("tagName"), "Query", p_node.attrib.get("type", "String"), req)
            sub = p_node.find("Parameters")
            if sub is not None: self._parse_parameters(sub, param.sub_parameters)
            target_dict[name] = param

    def flatten_params(self, input_params: Dict[str, Any], api_def: ApiDefinition, use_json: bool = True) -> Dict[str, Any]:
        flattened = {}
        def get_key(p: ApiParameter):
            n_clean = p.name.replace("data.", "")
            return n_clean if ("Set" in n_clean and "Set" not in p.tag_name) else (p.tag_name or n_clean)

        def to_wire(data: Any, defs: Dict[str, ApiParameter]) -> Any:
            if isinstance(data, dict):
                res = {}
                for k, v in data.items():
                    target = None
                    for pn, pd in defs.items():
                        if pn == k or pd.tag_name == k or pn.split('.')[-1] == k.replace("data.", ""):
                            target = pd; break
                    res[get_key(target) if target else k.replace("data.", "")] = to_wire(v, target.sub_parameters if target else {})
                return res
            return [to_wire(i, defs) for i in data] if isinstance(data, list) else data

        def proc(data: Any, defs: Dict[str, ApiParameter], pref: str = ""):
            if isinstance(data, dict):
                for k, v in data.items():
                    target = None
                    for pn, pd in defs.items():
                        if pn == k or pd.tag_name == k or pn.split('.')[-1] == k.replace("data.", ""):
                            target = pd; break
                    if not target: flattened[pref + k.replace("data.", "")] = v; continue
                    tk = get_key(target)
                    if target.param_type == "RepeatList" and isinstance(v, list):
                        if use_json: flattened[tk] = json.dumps([to_wire(i, target.sub_parameters) for i in v], ensure_ascii=False)
                        else:
                            for i, item in enumerate(v, 1): proc(item, target.sub_parameters, f"{pref}{tk}.{i}.")
                    elif isinstance(v, (dict, list)):
                        if use_json and not pref: flattened[tk] = json.dumps(to_wire(v, target.sub_parameters), ensure_ascii=False)
                        else: proc(v, target.sub_parameters, f"{pref}{tk}.")
                    else: flattened[pref + tk] = v
        proc(input_params, api_def.parameters)
        return flattened

    def call_api(self, name: str, params: Dict[str, Any], path: str = None, use_json: bool = True):
        api_def = self.apis[name]
        final = self.flatten_params(params, api_def, use_json)
        final.update({"Action": name, "RequestId": str(uuid.uuid4())})
        if "instanceId" in final: final["InstanceId"] = final.pop("instanceId")
        url = f"{self.protocol}://{self.host}:{self.port}{path or self.gateway_paths.get(name, '/')}"
        try:
            r = requests.post(url, data=final, verify=False, timeout=30)
            prep = requests.models.PreparedRequest(); prep.prepare_url(url, final)
            return r, final, prep.url
        except Exception as e: return f"Error: {e}", final, url

# --- GUI 界面类 ---

class ApiGui:
    def __init__(self, root, client: BastionHostApiClient):
        self.root = root; self.client = client
        self.root.title("BastionHost API Test Tool (Web & Scenario Enhanced)")
        self.root.geometry("1250x850")
        self.current_api_name = None; self.config_file = "server_configs.json"
        self.server_configs = self.load_server_configs(); self.scenario_steps: List[ScenarioStep] = []

        # --- Top Config ---
        conf_f = ttk.LabelFrame(root, text="基本配置"); conf_f.pack(fill="x", padx=10, pady=5)
        r1 = ttk.Frame(conf_f); r1.pack(fill="x", padx=5, pady=2)
        ttk.Label(r1, text="配置列表:").pack(side="left")
        self.config_cb = ttk.Combobox(r1, values=list(self.server_configs.keys()), width=15, state="readonly"); self.config_cb.pack(side="left", padx=5)
        self.config_cb.bind("<<ComboboxSelected>>", self.on_config_selected)
        self.alias_ent = ttk.Entry(r1, width=12); self.alias_ent.pack(side="left", padx=2)
        ttk.Button(r1, text="保存", command=self.save_current_config, width=5).pack(side="left", padx=2)
        ttk.Label(r1, text=" Host:").pack(side="left")
        self.host_ent = ttk.Entry(r1, width=18); self.host_ent.insert(0, client.host); self.host_ent.pack(side="left", padx=2)
        ttk.Label(r1, text=" Port:").pack(side="left")
        self.port_ent = ttk.Entry(r1, width=6); self.port_ent.insert(0, str(client.port)); self.port_ent.pack(side="left", padx=2)
        self.proto_cb = ttk.Combobox(r1, values=["http", "https"], width=6); self.proto_cb.set(client.protocol); self.proto_cb.pack(side="left", padx=2)
        ttk.Label(r1, text=" 并发:").pack(side="left")
        self.concurrent_ent = ttk.Entry(r1, width=4); self.concurrent_ent.insert(0, "1"); self.concurrent_ent.pack(side="left", padx=2)
        self.strategy_cb = ttk.Combobox(r1, values=["固定值", "随机值"], width=8, state="readonly"); self.strategy_cb.set("随机值"); self.strategy_cb.pack(side="left", padx=2)
        self.json_var = tk.BooleanVar(value=True); ttk.Checkbutton(r1, text="JSON 模式", variable=self.json_var).pack(side="left", padx=5)

        r2 = ttk.Frame(conf_f); r2.pack(fill="x", padx=5, pady=5)
        ttk.Label(r2, text="Path:").pack(side="left")
        self.path_ent = ttk.Entry(r2); self.path_ent.pack(side="left", fill="x", expand=True, padx=5)

        # --- Main Layout ---
        paned = ttk.PanedWindow(root, orient=tk.HORIZONTAL); paned.pack(fill="both", expand=True, padx=10, pady=5)
        
        l_f = ttk.Frame(paned); paned.add(l_f, weight=1)
        ttk.Label(l_f, text="接口搜索:").pack(anchor="w")
        self.search_var = tk.StringVar(); self.search_var.trace("w", self.filter_apis)
        ttk.Entry(l_f, textvariable=self.search_var).pack(fill="x", pady=2)
        
        btn_f = ttk.Frame(l_f); btn_f.pack(fill="x")
        ttk.Button(btn_f, text="全选", command=self.select_all_visible, width=8).pack(side="left", padx=2)
        ttk.Button(btn_f, text="清空", command=self.clear_all_selected, width=8).pack(side="left", padx=2)

        self.api_tree = ttk.Treeview(l_f, columns=("check", "name"), show="headings")
        self.api_tree.heading("check", text="√"); self.api_tree.heading("name", text="接口名称")
        self.api_tree.column("check", width=35, stretch=False, anchor="center")
        self.api_tree.pack(side="left", fill="both", expand=True)
        self.api_tree.bind("<Button-1>", self.on_tree_click)
        self.api_tree.bind("<Double-Button-1>", self.on_api_double_click)

        r_f = ttk.Frame(paned); paned.add(r_f, weight=3)
        ttk.Label(r_f, text="请求参数 (JSON):").pack(anchor="w")
        self.param_text = scrolledtext.ScrolledText(r_f, height=12, font=("Consolas", 11)); self.param_text.pack(fill="x", pady=5)
        
        btn_bar = ttk.Frame(r_f); btn_bar.pack(fill="x")
        self.run_btn = ttk.Button(btn_bar, text="单次请求", command=self.run_task); self.run_btn.pack(side="left", padx=2)
        self.bulk_btn = ttk.Button(btn_bar, text="批量测试", command=self.run_bulk_test); self.bulk_btn.pack(side="left", padx=2)
        self.bulk_details_btn = ttk.Button(btn_bar, text="结果详情", command=self.show_bulk_details, state="disabled"); self.bulk_details_btn.pack(side="left", padx=2)
        self.export_html_btn = ttk.Button(btn_bar, text="导出报告", command=self.export_results_to_html, state="disabled"); self.export_html_btn.pack(side="left", padx=2)
        ttk.Button(btn_bar, text="本地CSV导入", command=self.import_scenario_csv).pack(side="left", padx=2)
        ttk.Button(btn_bar, text="网页平台导入", command=self.import_from_web).pack(side="left", padx=2)
        ttk.Button(btn_bar, text="自动填充", command=self.auto_fill_params).pack(side="left", padx=2)
        ttk.Button(btn_bar, text="清空日志", command=lambda: self.res_text.delete("1.0", tk.END)).pack(side="right")

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(r_f, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", padx=2, pady=2)

        self.res_text = scrolledtext.ScrolledText(r_f, font=("Consolas", 11), bg="#f5f5f5"); self.res_text.pack(fill="both", expand=True, pady=5)
        self.res_text.tag_config("success", foreground="#28a745"); self.res_text.tag_config("warning", foreground="#ffc107")
        self.res_text.tag_config("error", foreground="#dc3545"); self.res_text.tag_config("bold", font=("Consolas", 11, "bold"))

        self.all_apis = sorted(list(self.client.apis.keys())); self.selected_apis = set(); self.bulk_results = []
        self.filter_apis()

    def filter_apis(self, *args):
        search_raw = self.search_var.get().strip().lower()
        # 支持空格、逗号、换行符分割多个关键字
        keywords = [k.strip() for k in re.split(r'[\s,\n]+', search_raw) if k.strip()]
        
        for item in self.api_tree.get_children(): self.api_tree.delete(item)
        for api in self.all_apis:
            api_l = api.lower()
            # 如果没有关键字，显示所有；如果有，匹配其中任意一个关键字即可显示
            if not keywords or any(k in api_l for k in keywords):
                self.api_tree.insert("", "end", iid=api, values=("☑" if api in self.selected_apis else "☐", api))

    def on_tree_click(self, event):
        item_id = self.api_tree.identify_row(event.y)
        if not item_id: return
        if self.api_tree.identify_column(event.x) == "#1":
            if item_id in self.selected_apis: self.selected_apis.remove(item_id)
            else: self.selected_apis.add(item_id)
            self.filter_apis()
        else:
            self.current_api_name = item_id; api_def = self.client.apis[item_id]
            self.path_ent.delete(0, tk.END); self.path_ent.insert(0, self.client.gateway_paths.get(item_id, "/"))
            template = {pn: "" for pn, pd in api_def.parameters.items() if pd.required}
            self.param_text.delete("1.0", tk.END); self.param_text.insert("1.0", json.dumps(template, indent=2, ensure_ascii=False))

    def on_api_double_click(self, event):
        item_id = self.api_tree.identify_row(event.y)
        if item_id and os.name == 'nt': subprocess.run(['explorer', '/select,', self.client.apis[item_id].file_path])

    def log(self, msg, tag=None): self.res_text.insert(tk.END, str(msg) + "\n", tag); self.res_text.see(tk.END)

    def load_server_configs(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f: return json.load(f)
            except: pass
        return {}

    def save_current_config(self):
        name = self.alias_ent.get().strip() or f"{self.host_ent.get()}:{self.port_ent.get()}"
        self.server_configs[name] = {"host": self.host_ent.get(), "port": self.port_ent.get(), "proto": self.proto_cb.get(), "alias": self.alias_ent.get()}
        with open(self.config_file, "w", encoding="utf-8") as f: json.dump(self.server_configs, f, indent=2)
        self.config_cb["values"] = list(self.server_configs.keys()); messagebox.showinfo("Success", "配置已保存")

    def on_config_selected(self, event):
        cfg = self.server_configs[self.config_cb.get()]
        self.host_ent.delete(0, tk.END); self.host_ent.insert(0, cfg['host'])
        self.port_ent.delete(0, tk.END); self.port_ent.insert(0, cfg['port'])
        self.proto_cb.set(cfg['proto']); self.alias_ent.delete(0, tk.END); self.alias_ent.insert(0, cfg.get('alias', ''))

    def generate_mock_params(self, api_def: ApiDefinition) -> Dict[str, Any]:
        strategy = self.strategy_cb.get()
        def get_v(p: ApiParameter):
            n = p.name.lower()
            if p.param_type == "RepeatList":
                if not p.sub_parameters:
                    # 如果没有子参数，生成基础值列表 (修复基础 ID 列表格式错误)
                    return [random.randint(10, 99)] if strategy == "随机值" else [10]
                item = {sn: get_v(sd) for sn, sd in p.sub_parameters.items()}
                return [item]
            if "id" in n: return random.randint(10, 99) if strategy == "随机值" else 10
            if "ip" in n: return "10.0.0.1"
            if p.param_type == "Boolean": return True
            if p.param_type in ["Integer", "Long"]: return 1
            return "test_" + "".join(random.choices(string.digits, k=4))
        return {pn: get_v(pd) for pn, pd in api_def.parameters.items() if pd.required or pd.param_type == "RepeatList"}

    def auto_fill_params(self):
        if self.current_api_name:
            self.param_text.delete("1.0", tk.END)
            self.param_text.insert("1.0", json.dumps(self.generate_mock_params(self.client.apis[self.current_api_name]), indent=2, ensure_ascii=False))

    def run_task(self):
        if not self.current_api_name: return
        self.client.host, self.client.port, self.client.protocol = self.host_ent.get().strip(), int(self.port_ent.get()), self.proto_cb.get()
        api_n = self.current_api_name
        try: params = json.loads(self.param_text.get("1.0", tk.END))
        except: self.log("JSON 格式错误", "error"); return
        self.log(f"--- 请求: {api_n} ---", "bold")
        res, ap, url = self.client.call_api(api_n, params, path=self.path_ent.get().strip(), use_json=self.json_var.get())
        self.log(f"URL: {url}"); self.log(f"参数: {json.dumps(ap, ensure_ascii=False)}")
        if isinstance(res, str): self.log(res, "error")
        else:
            resp_data = {}
            try: resp_data = res.json()
            except: pass
            # 优先取业务 code，没有则取 HTTP 状态码
            biz_code = resp_data.get("code", res.status_code)
            tag = "success" if str(biz_code).startswith("2") else "warning"
            self.log(f"状态: {biz_code}", tag)
            if resp_data: self.log(f"响应: {json.dumps(resp_data, indent=2, ensure_ascii=False)}")
            else: self.log(f"响应: {res.text[:1000]}")

    def run_bulk_test(self):
        if not self.selected_apis: return
        api_names = sorted(list(self.selected_apis)); count = int(self.concurrent_ent.get()); strategy = self.strategy_cb.get()
        self.bulk_results = []
        def task():
            total = len(api_names) * count; completed = 0
            self.root.after(0, lambda: self.progress_var.set(0))
            self.log(f"--- 批量开始 (并发: {count}) ---", "bold")
            self.client.host, self.client.port, self.client.protocol = self.host_ent.get().strip(), int(self.port_ent.get()), self.proto_cb.get()
            for api_n in api_names:
                api_def = self.client.apis[api_n]; base_p = self.generate_mock_params(api_def)
                with ThreadPoolExecutor(max_workers=count) as exe:
                    futures = [exe.submit(self.client.call_api, api_n, base_p if (i==1 or strategy=="固定值") else self.generate_mock_params(api_def), use_json=self.json_var.get()) for i in range(1, count+1)]
                    for i, f in enumerate(futures, 1):
                        r, ap, url = f.result()
                        info = {"name": f"{api_n} (#{i})" if count>1 else api_n, "url": url, "params": ap, "status": "Error", "response": "", "error": ""}
                        msg = ""
                        if not isinstance(r, str):
                            try: 
                                info["response"] = r.json()
                                biz_code = info["response"].get("code", r.status_code)
                                info["status"] = str(biz_code)
                                msg = info["response"].get("message", "")
                            except: 
                                info["response"] = r.text
                                info["status"] = str(r.status_code)
                        else: info["error"] = r
                        self.bulk_results.append(info)
                        # 日志中增加详细的失败消息打印
                        log_msg = f" {info['name']}: {info['status']}"
                        if msg: log_msg += f" - {msg}"
                        self.log(log_msg, "success" if info["status"].startswith("2") else "warning")
                        completed += 1
                        self.root.after(0, lambda v=completed: self.progress_var.set((v/total)*100))
            self.root.after(0, lambda: [self.bulk_details_btn.config(state="normal"), self.export_html_btn.config(state="normal")])
        threading.Thread(target=task, daemon=True).start()

    def show_bulk_details(self):
        win = tk.Toplevel(self.root); win.title("测试结果详情"); win.geometry("1000x700")
        paned = ttk.PanedWindow(win, orient=tk.HORIZONTAL); paned.pack(fill="both", expand=True, padx=5, pady=5)
        tree = ttk.Treeview(paned, columns=("status", "name"), show="headings"); paned.add(tree, weight=1)
        tree.heading("status", text="状态"); tree.heading("name", text="名称")
        txt = scrolledtext.ScrolledText(paned, font=("Consolas", 10)); paned.add(txt, weight=3)
        def show_v(e):
            sel = tree.selection()
            if sel:
                r = self.bulk_results[int(sel[0])]
                txt.delete("1.0", tk.END); txt.insert(tk.END, f"API: {r['name']}\nURL: {r['url']}\n\n[打平参数]\n{json.dumps(r['params'], indent=2, ensure_ascii=False)}\n\n[响应内容]\n{json.dumps(r['response'], indent=2, ensure_ascii=False)}\n{r['error']}")
        tree.bind("<<TreeviewSelect>>", show_v)
        for i, r in enumerate(self.bulk_results): tree.insert("", "end", iid=str(i), values=(r['status'], r['name']))

    def import_scenario_csv(self):
        path = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
        if not path: return
        steps = []
        try:
            with open(path, "r", encoding='utf-8-sig') as f:
                rows = list(csv.reader(f))
                idx = next(i for i, r in enumerate(rows) if "测试编号" in r or "测试项" in r)
                for r in rows[idx+1:]:
                    if len(r) >= 5 and r[0]:
                        s = ScenarioStep(r[0], r[1], r[2], r[3], r[4])
                        s.mapped_api = self.guess_api(s); steps.append(s)
            self.scenario_steps = steps; self.show_scenario_manager()
        except Exception as e: messagebox.showerror("Error", f"加载失败: {e}")

    def import_from_web(self):
        web_win = tk.Toplevel(self.root); web_win.title("网页平台导入"); web_win.geometry("550x450")
        
        # --- 增加地址配置 ---
        url_f = ttk.Frame(web_win); url_f.pack(fill="x", padx=10, pady=10)
        ttk.Label(url_f, text="平台地址:").pack(side="left")
        
        # 从全局配置中获取上次的 Web 地址
        saved_url = self.server_configs.get("_last_web_url", "http://192.168.0.107:5000")
        url_ent = ttk.Entry(url_f, width=40); url_ent.insert(0, saved_url); url_ent.pack(side="left", padx=5)
        
        ttk.Label(web_win, text="1. 选择功能模块 (Folder):").pack(pady=5, padx=10, anchor="w")
        folder_cb = ttk.Combobox(web_win, state="readonly", width=50); folder_cb.pack(pady=5, padx=10)
        
        ttk.Label(web_win, text="2. 选择用例文件 (File):").pack(pady=5, padx=10, anchor="w")
        file_cb = ttk.Combobox(web_win, state="readonly", width=50); file_cb.pack(pady=5, padx=10)
        
        status_lbl = ttk.Label(web_win, text="请输入地址并点击连接", foreground="gray"); status_lbl.pack(pady=5)

        file_map = {} 

        def load_folders():
            base_url = url_ent.get().strip().rstrip('/')
            if not base_url: return
            status_lbl.config(text="正在连接...", foreground="blue")
            try:
                resp = requests.get(f"{base_url}/api/folders", timeout=5)
                folders = resp.json().get("folders", [])
                folder_cb["values"] = folders
                status_lbl.config(text=f"连接成功，加载 {len(folders)} 个模块", foreground="green")
                # 保存地址到配置
                self.server_configs["_last_web_url"] = base_url
                with open(self.config_file, "w", encoding="utf-8") as f: json.dump(self.server_configs, f, indent=2)
            except Exception as e:
                status_lbl.config(text=f"连接失败: {e}", foreground="red")

        def on_folder_selected(event):
            base_url = url_ent.get().strip().rstrip('/')
            folder = folder_cb.get()
            file_cb.set(""); file_cb["values"] = []; file_map.clear()
            status_lbl.config(text=f"正在获取 {folder} 的文件列表...", foreground="blue")
            try:
                resp = requests.get(f"{base_url}/api/files", params={"folder": folder}, timeout=5)
                data = resp.json()
                raw_files = data.get("files", [])
                labels = [f.get("label", "未命名") for f in raw_files]
                for f in raw_files: file_map[f.get("label")] = f.get("name")
                
                file_cb["values"] = labels
                status_lbl.config(text=f"发现 {len(labels)} 个文件", foreground="green")
            except Exception as e:
                status_lbl.config(text=f"获取失败: {e}", foreground="red")

        def do_import():
            base_url = url_ent.get().strip().rstrip('/')
            folder = folder_cb.get()
            label = file_cb.get()
            filename = file_map.get(label, label)
            
            if not folder or not label:
                messagebox.showwarning("提示", "请先选择模块和文件")
                return
            
            status_lbl.config(text=f"正在解析 [{label}] ...", foreground="blue")
            try:
                resp = requests.post(f"{base_url}/api/preview", json={"folder": folder, "file": filename}, timeout=10)
                data = resp.json()
                rows = data.get("rows", [])
                # ... 转换 ScenarioStep (保持原有逻辑) ...
                steps = []
                for r in rows:
                    s = ScenarioStep(r.get("uid"), r.get("所属模块", folder), r.get("测试项", "未命名"), r.get("步骤", ""), r.get("预期结果", ""))
                    s.mapped_api = self.guess_api(s)
                    steps.append(s)
                
                if steps:
                    self.scenario_steps = steps
                    web_win.destroy()
                    self.show_scenario_manager()
                else:
                    status_lbl.config(text="无可用条目", foreground="orange")
            except Exception as e:
                messagebox.showerror("Error", f"导入失败: {e}")

        ttk.Button(url_f, text="连接", command=load_folders).pack(side="left", padx=5)
        folder_cb.bind("<<ComboboxSelected>>", on_folder_selected)
        ttk.Button(web_win, text="确定导入", command=do_import).pack(pady=20)
        
        # 如果已有地址，自动尝试一次加载
        if saved_url: threading.Thread(target=load_folders, daemon=True).start()

    def guess_api(self, s: ScenarioStep):
        # 领域技术词典：将 API 常用单词映射到中文语义
        DOMAIN_DICT = {
            # 动作类
            "Create": ["新增", "创建", "新建", "添加"],
            "Delete": ["删除", "移除", "销毁", "清空"],
            "Update": ["修改", "更新", "编辑", "设置", "重置"],
            "Describe": ["查询", "查看", "列表", "详情", "获取", "搜索"],
            "List": ["列表", "清单"],
            "Import": ["导入", "上传"],
            "Export": ["导出", "下载"],
            "Bind": ["绑定", "关联"],
            "Unbind": ["解绑", "取消关联"],
            "Modify": ["修改", "变更"],
            # 对象类
            "Role": ["角色", "权限组"],
            "User": ["用户", "账号", "账户", "人员"],
            "Host": ["主机", "资产", "服务器", "资源"],
            "Group": ["组", "集群"],
            "Policy": ["策略", "控制", "规则"],
            "Audit": ["审计", "日志", "回放"],
            "Session": ["会话", "连接"],
            "Password": ["密码", "密钥", "凭据"],
            "Command": ["命令", "指令"],
            "Application": ["应用", "代填"],
            "Instance": ["实例"],
            "Tag": ["标签", "分类"],
            "Config": ["配置", "设置"],
            "Auth": ["授权", "认证"],
            "Ticket": ["工单", "申请"]
        }

        # 获取用例内容
        content = (s.module + s.item + s.steps).lower()
        scores = []

        for api in self.all_apis:
            # 1. 拆解 CamelCase 接口名为单词列表 (例: DescribeHostGroup -> ['Describe', 'Host', 'Group'])
            tokens = re.findall(r'[A-Z][a-z0-9]*', api)
            if not tokens: continue
            
            match_count = 0
            # 2. 检查每个单词对应的中文是否出现在用例中
            for token in tokens:
                chn_synonyms = DOMAIN_DICT.get(token, [])
                if any(syn in content for syn in chn_synonyms):
                    match_count += 1
            
            # 3. 计算匹配得分：匹配到的单词数 / 接口总单词数 (占比越高越精准)
            # 同时给予动作单词 (tokens[0]) 额外权重，因为它是接口的核心意图
            score = (match_count / len(tokens)) * 10
            
            # 核心意图加强：如果第一个动作单词匹配到了，加分
            first_token_syns = DOMAIN_DICT.get(tokens[0], [])
            if any(syn in content for syn in first_token_syns):
                score += 5

            if score > 5: # 设定一个阈值，过滤掉低相关的匹配
                scores.append((score, api))

        # 降序排列，取最高分
        if scores:
            scores.sort(key=lambda x: x[0], reverse=True)
            return scores[0][1]
        return None

    def show_scenario_manager(self):
        mgr = tk.Toplevel(self.root); mgr.title("用例与接口映射"); mgr.geometry("1100x650")
        ttk.Button(mgr, text="开始场景测试", command=lambda: self.run_scenario_tests(mgr)).pack(pady=5)
        tree = ttk.Treeview(mgr, columns=("mod", "item", "api", "status"), show="headings"); tree.pack(fill="both", expand=True, padx=5)
        for c, t in zip(["mod", "item", "api", "status"], ["模块", "测试项", "对应接口 (双击搜索修改)", "状态"]): tree.heading(c, text=t)
        tree.column("item", width=350); tree.column("api", width=250)
        def refresh():
            for i in tree.get_children(): tree.delete(i)
            for i, s in enumerate(self.scenario_steps): tree.insert("", "end", iid=str(i), values=(s.module, s.item, s.mapped_api or "未匹配", s.status))
        def edit(e):
            row = tree.identify_row(e.y)
            if not row or tree.identify_column(e.x) != "#3": return
            sel_win = tk.Toplevel(mgr); ent = ttk.Entry(sel_win, width=40); ent.pack(padx=10, pady=5)
            lb = tk.Listbox(sel_win, width=60, height=15); lb.pack(padx=10, pady=5)
            def up(*a):
                lb.delete(0, tk.END)
                for api in self.all_apis:
                    if not ent.get() or ent.get().lower() in api.lower(): lb.insert(tk.END, api)
            ent.bind("<KeyRelease>", up); up()
            def cf():
                if lb.curselection(): self.scenario_steps[int(row)].mapped_api = lb.get(lb.curselection()); refresh(); sel_win.destroy()
            ttk.Button(sel_win, text="确定", command=cf).pack()
        tree.bind("<Double-Button-1>", edit); refresh()

    def run_scenario_tests(self, mgr):
        self.bulk_results = []
        def task():
            total = len(self.scenario_steps); completed = 0
            self.root.after(0, lambda: self.progress_var.set(0))
            self.client.host, self.client.port, self.client.protocol = self.host_ent.get().strip(), int(self.port_ent.get()), self.proto_cb.get()
            for i, s in enumerate(self.scenario_steps, 1):
                if not s.mapped_api: 
                    completed += 1
                    self.root.after(0, lambda v=completed: self.progress_var.set((v/total)*100))
                    continue
                api_def = self.client.apis[s.mapped_api]
                res, ap, url = self.client.call_api(s.mapped_api, self.generate_mock_params(api_def), use_json=self.json_var.get())
                
                resp_data = {}
                if not isinstance(res, str):
                    try: resp_data = res.json()
                    except: pass
                    biz_code = resp_data.get("code", res.status_code)
                    s.status = f"完成 ({biz_code})"
                else:
                    s.status = "Error"
                
                self.bulk_results.append({
                    "name": f"[{s.item}] {s.mapped_api}", 
                    "url": url, 
                    "params": ap, 
                    "status": str(resp_data.get("code", res.status_code)) if not isinstance(res, str) else "Error", 
                    "response": resp_data or (res.text if not isinstance(res, str) else ""), 
                    "error": res if isinstance(res, str) else "", 
                    "expected": s.expected, 
                    "api_desc": api_def.description
                })
                completed += 1
                self.root.after(0, lambda v=completed: self.progress_var.set((v/total)*100))
            
            self.root.after(0, lambda: [messagebox.showinfo("Success", "场景测试执行完毕"), mgr.destroy(), self.bulk_details_btn.config(state="normal"), self.export_html_btn.config(state="normal")])
        
        threading.Thread(target=task, daemon=True).start()

    def select_all_visible(self):
        for item in self.api_tree.get_children(): self.selected_apis.add(item)
        self.filter_apis()

    def clear_all_selected(self):
        self.selected_apis.clear(); self.filter_apis()

    def export_results_to_html(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"API_Report_{timestamp}.html"
        path = filedialog.asksaveasfilename(defaultextension=".html", initialfile=default_name)
        if not path: return
        rows = ""
        for r in self.bulk_results:
            st_cl = "green" if str(r['status']).startswith("2") else "red"
            rows += f"<tr><td><b>{r['name']}</b><br><small>{r.get('api_desc','')}</small></td><td style='color:{st_cl}'>{r['status']}</td><td>{r.get('expected','N/A')}</td><td><pre>{json.dumps(r['params'], indent=2, ensure_ascii=False)}</pre></td><td><pre>{json.dumps(r['response'], indent=2, ensure_ascii=False)}</pre></td></tr>"
        html = f"<html><head><style>body{{font-family:sans-serif;}} table{{width:100%;border-collapse:collapse;}} th,td{{border:1px solid #ddd;padding:8px;vertical-align:top;font-size:12px;}} th{{background:#007bff;color:white;}} pre{{background:#272822;color:#f8f8f2;padding:5px;max-height:300px;overflow:auto;}}</style></head><body><h1>测试报告</h1><table><tr><th width='15%'>用例/接口</th><th width='8%'>状态</th><th width='15%'>预期结果</th><th width='30%'>请求参数</th><th width='32%'>响应结果</th></tr>{rows}</table></body></html>"
        with open(path, "w", encoding="utf-8") as f: f.write(html)
        if messagebox.askyesno("Success", "已生成报告，是否打开？"): os.startfile(path)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pop-dir", default="pop/Apis/2020-02-18"); p.add_argument("--gate-dir", default="api")
    args = p.parse_args()
    c = BastionHostApiClient("127.0.0.1", 8080); c.load_apis(args.pop_dir, args.gate_dir)
    root = tk.Tk(); app = ApiGui(root, c); root.mainloop()

if __name__ == "__main__": main()

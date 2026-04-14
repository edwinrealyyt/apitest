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
        self.params: Dict[str, Any] = {} # 用于存储修改后的参数
        self.selected: bool = True # 默认勾选状态

class BastionHostApiClient:
    def __init__(self, host: str, port: int, protocol: str = "http", base_path: str = "/"):
        self.host = host
        self.port = port
        self.protocol = protocol.lower()
        self.base_path = base_path
        self.apis: Dict[str, ApiDefinition] = {}
        self.gateway_paths: Dict[str, str] = {}
        self.global_context = {}

    def extract_context_from_response(self, response_data: Any):
        def _recurse(data):
            if isinstance(data, dict):
                for k, v in data.items():
                    if k.lower().endswith("id") and v is not None and not isinstance(v, (dict, list)):
                        self.global_context[k] = v
                        self.global_context[k.lower()] = v
                    _recurse(v)
            elif isinstance(data, list):
                for item in data:
                    _recurse(item)
        _recurse(response_data)

    def load_apis(self, pop_dir: str, gate_dir: str):
        for root, _, files in os.walk(pop_dir):
            for file in files:
                if file.endswith(".xml"): self._parse_pop_xml(os.path.join(root, file))
        for root, _, files in os.walk(gate_dir):
            for file in files:
                if file.endswith(".xml"): self._parse_gate_xml(os.path.join(root, file))

    def _parse_pop_xml(self, file_path: str):
        try:
            root = ET.parse(file_path).getroot()
            if root.tag != "Api": return
            
            api_def = ApiDefinition(root.attrib.get("name"), root.attrib.get("version", ""), [], [], file_path)
            api_def.description = root.attrib.get("description", "")
            
            params_node = root.find("Parameters")
            if params_node is not None: 
                self._parse_parameters(params_node, api_def.parameters)
            self.apis[api_def.name] = api_def
        except Exception as e:
            print(f"Error parsing {file_path}: {e}")

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
        # 1. 强力黑名单（全小写，涵盖所有已知系统干扰项）
        SYSTEM_BLACKLIST = {
            "requestid", "callertype", "calluuid", "calleruid", 
            "callersigned", "callerbid", "regionid", "accesskeyid", "signature",
            "signaturemethod", "signaturenonce", "signatureversion", "timestamp", "version",
            "callerparentid", "securitytoken", "appip", "sourceip", "lang",
            "callersecuritytransport", "proxycallerip", "proxycallersecuritytransport",
            "proxytrusttransportinfo", "mfapresent", "ststokencalleruid",
            "ststokencallerbid", "ststokenprincipalid", "ststokenroleid", "ststokenuserid"
        }
        
        for p_node in parent_node.findall("Parameter"):
            name = p_node.attrib.get("name", "")
            tag_name = p_node.attrib.get("tagName", "")
            tag_pos = p_node.attrib.get("tagPosition", "")
            
            # 2. 字段名清洗逻辑
            raw_key = tag_name if tag_name else name
            clean_key = raw_key.replace("data.", "")
            
            # 3. 过滤逻辑：
            # - 跳过黑名单中的字段
            # - 跳过 tagPosition 为 System 的系统字段
            # - 跳过空字段或已存在的字段
            if clean_key.lower() in SYSTEM_BLACKLIST or tag_pos == "System":
                continue
            if not clean_key or clean_key in target_dict:
                continue

            req = p_node.attrib.get("required", "false").lower() in ["true", "ture"]
            param = ApiParameter(name, tag_name, "Query", p_node.attrib.get("type", "String"), req)
            param.description = p_node.attrib.get("description", "")
            
            sub = p_node.find("Parameters")
            if sub is not None: 
                self._parse_parameters(sub, param.sub_parameters)
            
            target_dict[clean_key] = param

    def flatten_params(self, input_params: Dict[str, Any], api_def: ApiDefinition, use_json: bool = True) -> Dict[str, Any]:
        flattened = {}
        
        def get_key(p: ApiParameter, fallback: str):
            if not p: return fallback.replace("data.", "")
            n_clean = p.name.replace("data.", "")
            t_name = p.tag_name
            
            # 纠偏逻辑：如果原始名称包含 Set 而 tagName 不包含，优先用原始名称
            # 解决如 UpdateSourceAuth 中 AuthModuleSet 被错误映射为 AuthModule 的问题
            if t_name and "Set" in n_clean and "Set" not in t_name:
                return n_clean
            
            return t_name if t_name else n_clean

        def map_obj_keys(data: Any, defs: Dict[str, ApiParameter]) -> Any:
            """仅用于 JSON 序列化前的字段名映射"""
            if isinstance(data, dict):
                return {get_key(next((pd for pn, pd in defs.items() if pn == k or pd.tag_name == k or pn.split('.')[-1] == k.replace("data.", "")), None), k): 
                        map_obj_keys(v, next((pd.sub_parameters for pn, pd in defs.items() if pn == k or pd.tag_name == k or pn.split('.')[-1] == k.replace("data.", "")), {})) 
                        for k, v in data.items()}
            if isinstance(data, list):
                return [map_obj_keys(i, defs) for i in data]
            return data

        def proc(data: Any, defs: Dict[str, ApiParameter], pref: str = ""):
            if isinstance(data, dict):
                for k, v in data.items():
                    target = None
                    for pn, pd in defs.items():
                        if pn == k or pd.tag_name == k or pn.split('.')[-1] == k.replace("data.", ""):
                            target = pd; break
                    
                    tk = get_key(target, k)
                    full_key = pref + tk
                    
                    if isinstance(v, list):
                        if use_json:
                            # 关键：一旦是列表且开启 JSON 模式，立即映射并序列化，不再向下 proc
                            mapped_list = map_obj_keys(v, target.sub_parameters if target else {})
                            flattened[full_key] = json.dumps(mapped_list, ensure_ascii=False)
                        else:
                            # 索引模式：AuthModuleSet.1.xxx
                            for i, item in enumerate(v, 1):
                                if isinstance(item, dict):
                                    proc(item, target.sub_parameters if target else {}, f"{full_key}.{i}.")
                                else:
                                    flattened[f"{full_key}.{i}"] = item
                    elif isinstance(v, dict):
                        if use_json and not pref:
                            # 顶层对象且 JSON 模式
                            mapped_dict = map_obj_keys(v, target.sub_parameters if target else {})
                            flattened[full_key] = json.dumps(mapped_dict, ensure_ascii=False)
                        else:
                            proc(v, target.sub_parameters if target else {}, f"{full_key}.")
                    else:
                        flattened[full_key] = v

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
        self.success_params_file = "success_params.json"
        self.server_configs = self.load_server_configs(); self.scenario_steps: List[ScenarioStep] = []
        self.last_error_message = ""; self.last_api_name = ""
        self.success_params = self.load_success_params()

        # --- Top Config ---
        # ... 原有 UI 逻辑开始 ...
        conf_f = ttk.LabelFrame(root, text="基本配置"); conf_f.pack(fill="x", padx=10, pady=5)
        r1 = ttk.Frame(conf_f); r1.pack(fill="x", padx=5, pady=2)
        ttk.Label(r1, text="配置列表:").pack(side="left")
        self.config_cb = ttk.Combobox(r1, values=[k for k in self.server_configs.keys() if not k.startswith("_")], width=15, state="readonly"); self.config_cb.pack(side="left", padx=5)
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
        self.export_html_btn = ttk.Button(btn_bar, text="导出报告", command=self.export_results, state="disabled"); self.export_html_btn.pack(side="left", padx=2)
        ttk.Button(btn_bar, text="本地CSV导入", command=self.import_scenario_csv).pack(side="left", padx=2)
        ttk.Button(btn_bar, text="网页平台导入", command=self.import_from_web).pack(side="left", padx=2)
        ttk.Button(btn_bar, text="自动填充", command=self.auto_fill_params).pack(side="left", padx=2)
        ttk.Button(btn_bar, text="生成 CRUD 链路", command=self.generate_crud_scenario).pack(side="left", padx=2)
        self.iter_var = tk.BooleanVar(value=False); ttk.Checkbutton(btn_bar, text="迭代模式", variable=self.iter_var).pack(side="left", padx=5)
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
        keywords = [k.strip() for k in re.split(r'[\s,\n]+', search_raw) if k.strip()]
        for item in self.api_tree.get_children(): self.api_tree.delete(item)
        for api in self.all_apis:
            api_l = api.lower()
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
            
            # 首先生成一个拥有完整、正确嵌套层级的参数树
            template = self.generate_mock_params(api_def)
            
            # 点击接口时，如果有历史记录，则强制全量恢复历史（覆盖掉刚才生成的随机业务字段）
            if item_id in self.success_params:
                self._deep_update(template, self.success_params[item_id])
                
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

    def load_success_params(self):
        if os.path.exists(self.success_params_file):
            try:
                with open(self.success_params_file, "r", encoding="utf-8") as f: return json.load(f)
            except: pass
        return {}

    def save_success_params(self, api_name, params):
        self.success_params[api_name] = params
        with open(self.success_params_file, "w", encoding="utf-8") as f: json.dump(self.success_params, f, indent=2, ensure_ascii=False)

    def generate_mock_params(self, api_def: ApiDefinition, include_optional: bool = True) -> Dict[str, Any]:
        strategy = self.strategy_cb.get()
        def get_v(p: ApiParameter):
            if p.param_type == "RepeatList":
                if not p.sub_parameters: 
                    return [random.randint(10, 99)] if strategy == "随机值" else [10]
                return [{sn: get_v(sd) for sn, sd in p.sub_parameters.items()}]

            if not p.required and not include_optional:
                if p.param_type == "Integer": return 0
                if p.param_type == "Boolean": return False
                return ""

            return self.generate_single_param(p)

        # 生成包含所有字段的基础模板
        params = {pn: get_v(pd) for pn, pd in api_def.parameters.items()}

        # 融合历史成功的参数
        if api_def.name in self.success_params:
            history = self.success_params[api_def.name]
            if strategy == "固定值":
                self._deep_update(params, history)
            else:
                # 随机值模式：仅从历史中恢复 InstanceId 等“环境依赖”字段
                self._deep_update_selective(params, history)
        return params

    def _deep_update_selective(self, target, source):
        """选择性更新：在随机模式下，仅恢复 ID 类关键环境参数"""
        ESSENTIAL_KEYS = {"instanceid", "regionid", "aliuid", "calleruid"}
        for k, v in source.items():
            if k in target:
                k_low = k.lower()
                if isinstance(v, dict) and isinstance(target[k], dict):
                    self._deep_update_selective(target[k], v)
                else:
                    # 环境关键 ID，或 target 当前为空，才从历史恢复
                    if any(ek in k_low for ek in ESSENTIAL_KEYS) or target[k] in ["", 0, None, []]:
                        target[k] = v
    def _deep_update(self, target, source):
        """深度更新：仅当键在 target 中存在时，才用 source 的值覆盖"""
        for k, v in source.items():
            # 只有当该键是当前 API 定义中合法的业务参数时，才允许从历史记录恢复
            if k in target:
                if isinstance(v, dict) and isinstance(target[k], dict):
                    self._deep_update(target[k], v)
                else:
                    target[k] = v
            # 如果 k 不在 target 中，说明它是已被过滤的系统参数或旧版残留的 data. 参数，直接丢弃，不再合入

    def _deep_complement(self, target, source):
        """深度补全：将 source 中缺失或为空的字段补到 target"""
        for k, v in source.items():
            if k not in target or target[k] == "" or target[k] == [] or target[k] is None:
                target[k] = v
            elif isinstance(v, dict) and isinstance(target[k], dict):
                self._deep_complement(target[k], v)

    def auto_fill_params(self):
        if not self.current_api_name: return
        api_def = self.client.apis[self.current_api_name]
        
        if self.iter_var.get():
            # 同步当前 UI 配置并启动迭代
            self.client.host, self.client.port, self.client.protocol = self.host_ent.get().strip(), int(self.port_ent.get()), self.proto_cb.get()
            path = self.path_ent.get().strip()
            use_json = self.json_var.get()
            try: current_params = json.loads(self.param_text.get("1.0", tk.END))
            except: current_params = self.generate_mock_params(api_def)
            threading.Thread(target=self.smart_fix_loop, args=(api_def, current_params, path, use_json), daemon=True).start()
        else:
            try: current_params = json.loads(self.param_text.get("1.0", tk.END))
            except: current_params = {}
            
            # 生成完整模板（含非必需参数 + 历史成功值）
            full_template = self.generate_mock_params(api_def, include_optional=True)
            
            if not current_params:
                current_params = full_template
            else:
                # 补全当前文本框 JSON 中缺失的字段
                self._deep_complement(current_params, full_template)
            
            if self.last_error_message and self.last_api_name == api_def.name:
                self.apply_fix(api_def, current_params, self.last_error_message)
            
            self.param_text.delete("1.0", tk.END)
            self.param_text.insert("1.0", json.dumps(current_params, indent=2, ensure_ascii=False))

    def apply_fix(self, api_def, current_params, msg):
        self.log(f"尝试修复: {msg}", "bold")
        
        # 1. 匹配缺失字段
        missing_match = re.search(r'([A-Za-z0-9_]+)\s*(?:缺失|不能为空|Required|is mandatory)', msg)
        if missing_match:
            field = missing_match.group(1)
            for pn, pd in api_def.parameters.items():
                if pn.lower() == field.lower() or (pd.tag_name and pd.tag_name.lower() == field.lower()):
                    current_params[pn] = self.generate_single_param(pd)
                    self.log(f"已补全字段: {pn}", "success"); return True
        
        # 2. 匹配枚举值错误
        enum_match = re.search(r'([A-Za-z0-9_]+)\s*(?:仅支持|only supports)\s*([\w:/,.-]+)', msg)
        if enum_match:
            field, val_part = enum_match.groups()
            nums = re.findall(r'\d+', val_part)
            if nums:
                for pn in api_def.parameters:
                    if pn.lower() == field.lower():
                        current_params[pn] = int(nums[0])
                        self.log(f"已修正枚举字段 {pn} 为: {nums[0]}", "success"); return True

        # 3. 匹配 MAC 格式错误 (适配 Pydantic 报错格式)
        if "MAC 地址格式" in msg or "valid MAC address" in msg.lower():
            # 提取字段名，如 'start_mac': 'test_2079' 中的 start_mac
            mac_fields = re.findall(r"['\"]([A-Za-z0-9_]*mac)['\"]:", msg, re.IGNORECASE)
            if not mac_fields:
                mac_fields = re.findall(r'([A-Za-z0-9_]*mac)\s*[:=]', msg, re.IGNORECASE)
            
            if mac_fields:
                fixed_any = False
                def deep_fix(data):
                    nonlocal fixed_any
                    if isinstance(data, dict):
                        for k, v in list(data.items()):
                            if any(mf.lower() == k.lower() for mf in mac_fields):
                                data[k] = ":".join(["%02x" % random.randint(0, 255) for _ in range(6)])
                                self.log(f"已修正 MAC 格式字段: {k}", "success")
                                fixed_any = True
                            deep_fix(v)
                    elif isinstance(data, list):
                        for item in data: deep_fix(item)
                
                deep_fix(current_params)
                if fixed_any: return True
        
        return False

    def smart_fix_loop(self, api_def, current_params, path, use_json):
        self.log("--- 开启迭代修复模式 ---", "bold")
        
        for i in range(10): # 最多重试10次
            self.root.after(0, lambda p=current_params: [self.param_text.delete("1.0", tk.END), self.param_text.insert("1.0", json.dumps(p, indent=2, ensure_ascii=False))])
            
            res, ap, url = self.client.call_api(api_def.name, current_params, path=path, use_json=use_json)
            if isinstance(res, str): self.log(f"网络错误: {res}", "error"); break
            
            try: resp_data = res.json()
            except: resp_data = {"code": res.status_code, "message": res.text}
            
            biz_code = str(resp_data.get("code", res.status_code))
            msg = resp_data.get("message", "")
            
            if biz_code.startswith("2"):
                self.log(f"迭代成功! 状态码: {biz_code}", "success")
                self.save_success_params(api_def.name, current_params)
                break
            
            self.log(f"迭代 {i+1} 失败: {msg}", "warning")
            if not self.apply_fix(api_def, current_params, msg):
                self.log("无法自动识别报错模式，停止迭代", "error"); break
            import time; time.sleep(0.5)

    def generate_single_param(self, p: ApiParameter):
        # 1. 优先从全局上下文缓存中获取
        if p.name in self.client.global_context:
            return self.client.global_context[p.name]
        if p.name.lower() in self.client.global_context:
            return self.client.global_context[p.name.lower()]

        n = p.name.lower()
        desc = p.description or ""

        # 2. 从描述中尝试解析枚举值 (正则匹配：支持/可选值/枚举 等关键字后的选项)
        # 匹配模式如：仅支持 Linux/Windows, 可选值：1,2,3, 包含：[a, b, c]
        enum_patterns = [
            r'(?:支持|可选值|枚举|包含|取值)[:：\s]*([a-zA-Z0-9_/，,\|（）\(\)\s]+)',
            r'\[\s*([a-zA-Z0-9_/，,\|（）\(\)\s]+)\s*\]'
        ]
        for pattern in enum_patterns:
            match = re.search(pattern, desc)
            if match:
                raw_vals = re.split(r'[/，,\|、\s]+', match.group(1).strip())
                # 过滤掉括号、空值和非业务词汇
                vals = [v.strip() for v in raw_vals if v.strip() and not re.match(r'^[（\(\)）]$', v)]
                if vals:
                    choice = random.choice(vals)
                    # 如果参数类型是整数，尝试转换提取到的值
                    if p.param_type == "Integer":
                        num_match = re.search(r'\d+', choice)
                        if num_match: return int(num_match.group())
                    return choice

        # 3. 语义推断逻辑
        if "os" in n or "system" in n: return random.choice(["Linux", "Windows"])
        if "email" in n: return f"test_{random.randint(100,999)}@example.com"
        if "phone" in n or "mobile" in n: return "138" + "".join(random.choices(string.digits, k=8))
        if "ip" in n: return f"10.0.{random.randint(0,255)}.{random.randint(1,254)}"
        if "port" in n: return random.choice([22, 80, 443, 3389, 3306])
        if "mac" in n: return ":".join(["%02x" % random.randint(0, 255) for _ in range(6)])
        if "id" in n: return 1

        # 4. 基于数据类型的兜底
        if p.param_type == "Integer": return 1
        if p.param_type == "Boolean": return True
        return "test_" + "".join(random.choices(string.digits, k=4))

    def run_task(self):
        if not self.current_api_name: return
        self.client.host, self.client.port, self.client.protocol = self.host_ent.get().strip(), int(self.port_ent.get()), self.proto_cb.get()
        api_n = self.current_api_name
        try: params = json.loads(self.param_text.get("1.0", tk.END))
        except: self.log("JSON 格式错误", "error"); return
        self.log(f"--- 请求: {api_n} ---", "bold")
        res, ap, url = self.client.call_api(api_n, params, path=self.path_ent.get().strip(), use_json=self.json_var.get())
        
        # 恢复 URL 和参数的日志详情
        self.log(f"URL: {url}")
        self.log(f"打平参数: {json.dumps(ap, ensure_ascii=False)}")

        if isinstance(res, str): self.log(res, "error")
        else:
            resp_data = {}
            try: resp_data = res.json()
            except: pass
            biz_code = resp_data.get("code", res.status_code)
            if str(biz_code).startswith("2"):
                self.last_error_message = ""
                self.save_success_params(api_n, params)
                self.client.extract_context_from_response(resp_data)
            else:
                self.last_error_message = resp_data.get("message", "")
                self.last_api_name = api_n
            tag = "success" if str(biz_code).startswith("2") else "warning"
            self.log(f"状态: {biz_code}", tag)
            self.log(f"响应: {json.dumps(resp_data, ensure_ascii=False)}")

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
                                if info["status"].startswith("2"):
                                    self.client.extract_context_from_response(info["response"])
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
                        s.mapped_api = self.guess_api(s)
                        if s.mapped_api:
                            s.params = self.generate_mock_params(self.client.apis[s.mapped_api])
                        steps.append(s)
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
            folder, label = folder_cb.get(), file_cb.get()
            filename = file_map.get(label, label)
            if not folder or not label: return
            try:
                resp = requests.post(f"{base_url}/api/preview", json={"folder": folder, "file": filename}, timeout=10)
                rows = resp.json().get("rows", [])
                steps = []
                for r in rows:
                    s = ScenarioStep(r.get("uid"), r.get("所属模块", folder), r.get("测试项", "未命名"), r.get("步骤", ""), r.get("预期结果", ""))
                    s.mapped_api = self.guess_api(s)
                    if s.mapped_api: # 自动生成初始参数
                        s.params = self.generate_mock_params(self.client.apis[s.mapped_api])
                    steps.append(s)
                if steps: self.scenario_steps = steps; web_win.destroy(); self.show_scenario_manager()
            except Exception as e: messagebox.showerror("Error", f"导入失败: {e}")

        ttk.Button(url_f, text="连接", command=load_folders).pack(side="left", padx=5)
        folder_cb.bind("<<ComboboxSelected>>", on_folder_selected)
        ttk.Button(web_win, text="确定导入", command=do_import).pack(pady=20)
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
            
            # 3. 计算匹配得分
            score = (match_count / len(tokens)) * 10
            
            # 核心意图加强
            first_token_syns = DOMAIN_DICT.get(tokens[0], [])
            if any(syn in content for syn in first_token_syns):
                score += 5

            if score > 5:
                scores.append((score, api))

        # 降序排列，取最高分
        if scores:
            scores.sort(key=lambda x: x[0], reverse=True)
            return scores[0][1]
        return None

    def show_scenario_manager(self):
        mgr = tk.Toplevel(self.root); mgr.title("用例管理 (勾选执行/双击编辑)"); mgr.geometry("1200x700")
        top_f = ttk.Frame(mgr); top_f.pack(fill="x", pady=5)
        ttk.Button(top_f, text="全选", command=lambda: self.toggle_scenario_selection(True, refresh_fn)).pack(side="left", padx=5)
        ttk.Button(top_f, text="取消全选", command=lambda: self.toggle_scenario_selection(False, refresh_fn)).pack(side="left", padx=5)
        ttk.Button(top_f, text="开始执行勾选项", command=lambda: self.run_scenario_tests(mgr)).pack(side="right", padx=10)
        
        tree = ttk.Treeview(mgr, columns=("check", "mod", "item", "api", "params"), show="headings"); tree.pack(fill="both", expand=True, padx=5)
        for c, t in zip(["check", "mod", "item", "api", "params"], ["√", "模块", "测试项", "对应接口 (双击搜索)", "参数预览 (双击修改)"]):
            tree.heading(c, text=t)
        tree.column("check", width=35, anchor="center"); tree.column("item", width=300); tree.column("api", width=200); tree.column("params", width=400)

        def refresh_fn():
            for i in tree.get_children(): tree.delete(i)
            for i, s in enumerate(self.scenario_steps):
                p_str = json.dumps(s.params, ensure_ascii=False)
                tree.insert("", "end", iid=str(i), values=("☑" if s.selected else "☐", s.module, s.item, s.mapped_api or "未匹配", p_str))

        def on_click(e):
            row = tree.identify_row(e.y); col = tree.identify_column(e.x)
            if row and col == "#1": # 切换勾选
                idx = int(row)
                self.scenario_steps[idx].selected = not self.scenario_steps[idx].selected
                refresh_fn()

        def on_double_click(e):
            row = tree.identify_row(e.y); col = tree.identify_column(e.x)
            if not row: return
            idx = int(row)
            if col == "#4": self.open_api_selector(mgr, idx, refresh_fn)
            elif col == "#5": self.open_param_editor(mgr, idx, refresh_fn)

        tree.bind("<Button-1>", on_click); tree.bind("<Double-Button-1>", on_double_click); refresh_fn()

    def toggle_scenario_selection(self, state, refresh_fn):
        for s in self.scenario_steps: s.selected = state
        refresh_fn()

    def open_api_selector(self, parent, idx, refresh_fn):
        sel_win = tk.Toplevel(parent); sel_win.title("选择接口"); ent = ttk.Entry(sel_win, width=40); ent.pack(padx=10, pady=5)
        lb = tk.Listbox(sel_win, width=60, height=15); lb.pack(padx=10, pady=5)
        def up(*a):
            lb.delete(0, tk.END)
            for api in self.all_apis:
                if not ent.get() or ent.get().lower() in api.lower(): lb.insert(tk.END, api)
        ent.bind("<KeyRelease>", up); up()
        def cf():
            if lb.curselection():
                api_n = lb.get(lb.curselection())
                self.scenario_steps[idx].mapped_api = api_n
                self.scenario_steps[idx].params = self.generate_mock_params(self.client.apis[api_n])
                refresh_fn(); sel_win.destroy()
        ttk.Button(sel_win, text="确定", command=cf).pack(pady=5)

    def open_param_editor(self, parent, idx, refresh_fn):
        edit_win = tk.Toplevel(parent); edit_win.title("编辑请求参数 (JSON)"); edit_win.geometry("600x500")
        txt = scrolledtext.ScrolledText(edit_win, font=("Consolas", 11)); txt.pack(fill="both", expand=True, padx=5, pady=5)
        txt.insert("1.0", json.dumps(self.scenario_steps[idx].params, indent=2, ensure_ascii=False))
        def save():
            try:
                self.scenario_steps[idx].params = json.loads(txt.get("1.0", tk.END))
                refresh_fn(); edit_win.destroy()
            except Exception as e: messagebox.showerror("Error", f"JSON 格式错误: {e}")
        ttk.Button(edit_win, text="保存参数", command=save).pack(pady=5)

    def run_scenario_tests(self, mgr):
        self.bulk_results = []
        def task():
            active_steps = [s for s in self.scenario_steps if s.selected and s.mapped_api]
            if not active_steps:
                self.root.after(0, lambda: messagebox.showwarning("提示", "没有选中的可执行用例"))
                return
            
            total, completed = len(active_steps), 0
            self.root.after(0, lambda: self.progress_var.set(0))
            self.client.host, self.client.port, self.client.protocol = self.host_ent.get().strip(), int(self.port_ent.get()), self.proto_cb.get()
            
            for i, s in enumerate(active_steps, 1):
                api_def = self.client.apis[s.mapped_api]
                res, ap, url = self.client.call_api(s.mapped_api, s.params, use_json=self.json_var.get())
                
                resp_data = {}
                if not isinstance(res, str):
                    try: resp_data = res.json()
                    except: pass
                    biz_code = resp_data.get("code", res.status_code)
                    s.status = f"完成 ({biz_code})"
                else: s.status = "Error"
                
                self.bulk_results.append({
                    "name": f"[{s.item}] {s.mapped_api}", "url": url, "params": ap, 
                    "status": str(resp_data.get("code", res.status_code)) if not isinstance(res, str) else "Error", 
                    "response": resp_data or (res.text if not isinstance(res, str) else ""), 
                    "error": res if isinstance(res, str) else "", "expected": s.expected, "api_desc": api_def.description
                })
                completed += 1
                self.root.after(0, lambda v=completed: self.progress_var.set((v/total)*100))
            
            self.root.after(0, lambda: [messagebox.showinfo("Success", f"执行完毕 ({total}项)"), mgr.destroy(), self.bulk_details_btn.config(state="normal"), self.export_html_btn.config(state="normal")])
        threading.Thread(target=task, daemon=True).start()

    def select_all_visible(self):
        for item in self.api_tree.get_children(): self.selected_apis.add(item)
        self.filter_apis()

    def clear_all_selected(self):
        self.selected_apis.clear(); self.filter_apis()

    def generate_crud_scenario(self):
        if not self.current_api_name:
            messagebox.showwarning("提示", "请先在左侧选择一个核心接口 (如 CreateHost)")
            return
        
        # 1. 提取核心实体名
        # 移除常见的动作前缀：Create, Delete, Update, Modify, Describe, List, Get
        entity = re.sub(r'^(Create|Delete|Update|Modify|Describe|List|Get)', '', self.current_api_name)
        if not entity:
            messagebox.showwarning("提示", f"无法从接口名 '{self.current_api_name}' 中识别实体")
            return

        # 2. 定义 CRUD 动作及其匹配模式
        actions = [
            ("Create", ["Create"]),
            ("Read/List", ["Describe", "List", "Get"]),
            ("Update", ["Modify", "Update"]),
            ("Delete", ["Delete"])
        ]

        steps = []
        for label, prefixes in actions:
            match_api = None
            for prefix in prefixes:
                potential_name = prefix + entity
                if potential_name in self.all_apis:
                    match_api = potential_name
                    break
            
            if match_api:
                s = ScenarioStep("", entity, f"{label}{entity}", f"执行 {match_api} 接口", "请求成功 (2xx)")
                s.mapped_api = match_api
                s.params = self.generate_mock_params(self.client.apis[match_api])
                steps.append(s)

        if not steps:
            messagebox.showwarning("提示", f"未找到与实体 '{entity}' 相关的 CRUD 链路接口")
            return

        # 3. 合入场景列表并打开管理器
        self.scenario_steps.extend(steps)
        self.show_scenario_manager()
        messagebox.showinfo("Success", f"已成功为实体 '{entity}' 生成 {len(steps)} 个链路步骤")

    def export_results(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"API_Report_{timestamp}"
        path = filedialog.asksaveasfilename(
            defaultextension=".html",
            filetypes=[("HTML Report", "*.html"), ("CSV Report", "*.csv")],
            initialfile=default_name
        )
        if not path: return

        if path.endswith(".csv"):
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["接口名称", "描述", "状态", "预期结果", "请求参数", "响应结果", "URL"])
                    for r in self.bulk_results:
                        writer.writerow([
                            r['name'],
                            r.get('api_desc', ''),
                            r['status'],
                            r.get('expected', 'N/A'),
                            json.dumps(r['params'], ensure_ascii=False),
                            json.dumps(r['response'], ensure_ascii=False),
                            r['url']
                        ])
                if messagebox.askyesno("Success", "CSV 报告已生成，是否打开？"): os.startfile(path)
            except Exception as e:
                messagebox.showerror("Error", f"导出 CSV 失败: {e}")
        else:
            rows = ""
            for r in self.bulk_results:
                st_cl = "green" if str(r['status']).startswith("2") else "red"
                rows += f"<tr><td><b>{r['name']}</b><br><small>{r.get('api_desc','')}</small></td><td style='color:{st_cl}'>{r['status']}</td><td>{r.get('expected','N/A')}</td><td><pre>{json.dumps(r['params'], indent=2, ensure_ascii=False)}</pre></td><td><pre>{json.dumps(r['response'], indent=2, ensure_ascii=False)}</pre></td></tr>"
            html = f"<html><head><style>body{{font-family:sans-serif;}} table{{width:100%;border-collapse:collapse;}} th,td{{border:1px solid #ddd;padding:8px;vertical-align:top;font-size:12px;}} th{{background:#007bff;color:white;}} pre{{background:#272822;color:#f8f8f2;padding:5px;max-height:300px;overflow:auto;}}</style></head><body><h1>测试报告</h1><table><tr><th width='15%'>用例/接口</th><th width='8%'>状态</th><th width='15%'>预期结果</th><th width='30%'>请求参数</th><th width='32%'>响应结果</th></tr>{rows}</table></body></html>"
            with open(path, "w", encoding="utf-8") as f: f.write(html)
            if messagebox.askyesno("Success", "HTML 报告已生成，是否打开？"): os.startfile(path)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pop-dir", default="pop/Apis/2020-02-18"); p.add_argument("--gate-dir", default="api")
    args = p.parse_args()
    c = BastionHostApiClient("127.0.0.1", 8080); c.load_apis(args.pop_dir, args.gate_dir)
    root = tk.Tk(); app = ApiGui(root, c); root.mainloop()

if __name__ == "__main__": main()

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
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

# 忽略 HTTPS 警告
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 核心逻辑类 ---

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
        self.parameters: Dict[str, ApiParameter] = {}

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
                if file.endswith(".xml"):
                    self._parse_pop_xml(os.path.join(root, file))
        for root, _, files in os.walk(gate_dir):
            for file in files:
                if file.endswith(".xml"):
                    self._parse_gate_xml(os.path.join(root, file))

    def _parse_pop_xml(self, file_path: str):
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            if root.tag != "Api": return
            api_name = root.attrib.get("name")
            version = root.attrib.get("version", "")
            isv_protocol = root.find("IsvProtocol")
            if isv_protocol is None: return
            protocols = isv_protocol.attrib.get("protocol", "HTTP").split("|")
            methods = isv_protocol.attrib.get("method", "GET").split("|")
            api_def = ApiDefinition(api_name, version, protocols, methods, file_path)
            params_node = root.find("Parameters")
            if params_node is not None:
                self._parse_parameters(params_node, api_def.parameters)
            self.apis[api_name] = api_def
        except: pass

    def _parse_gate_xml(self, file_path: str):
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            if root.tag.lower() != "api": return
            name_node = root.find("name")
            api_name = name_node.text if name_node is not None else None
            if not api_name: return
            for prop in root.findall(".//property"):
                if prop.attrib.get("mapping") == "path":
                    path_val = prop.attrib.get("value")
                    if path_val:
                        self.gateway_paths[api_name] = path_val
                        break
        except: pass

    def _parse_parameters(self, parent_node: ET.Element, target_dict: Dict[str, ApiParameter]):
        for param_node in parent_node.findall("Parameter"):
            name = param_node.attrib.get("name")
            tag_name = param_node.attrib.get("tagName")
            tag_position = param_node.attrib.get("tagPosition", "Query")
            param_type = param_node.attrib.get("type", "String")
            req_str = param_node.attrib.get("required", "false").lower()
            required = req_str == "true" or req_str == "ture"
            description = param_node.attrib.get("description", "")
            example = param_node.attrib.get("example", "")
            param = ApiParameter(name, tag_name, tag_position, param_type, required, description, example)
            sub_params_node = param_node.find("Parameters")
            if sub_params_node is not None:
                self._parse_parameters(sub_params_node, param.sub_parameters)
            target_dict[name] = param
def flatten_params(self, input_params: Dict[str, Any], api_def: ApiDefinition, use_json_for_complex: bool = True) -> Dict[str, Any]:
    flattened = {}

    def get_clean_key(p_def: ApiParameter):
        """获取干净的键名。解决 XML 定义中 tagName 错误（如漏掉 Set）的问题。"""
        name_no_data = p_def.name.replace("data.", "")
        # 逻辑：如果原始 name 包含 Set 但 tagName 不包含，说明 tagName 定义可能有误，优先使用 name
        if "Set" in name_no_data and "Set" not in p_def.tag_name:
            return name_no_data
        return p_def.tag_name or name_no_data


        def transform_to_wire(data: Any, param_defs: Dict[str, ApiParameter]) -> Any:
            if isinstance(data, dict):
                new_dict = {}
                for k, v in data.items():
                    p_def = None
                    for p_name, pd in param_defs.items():
                        if p_name == k or pd.tag_name == k or p_name.split('.')[-1] == k.replace("data.", ""):
                            p_def = pd
                            break
                    new_k = get_clean_key(p_def) if p_def else k.replace("data.", "")
                    new_dict[new_k] = transform_to_wire(v, p_def.sub_parameters if p_def else {})
                return new_dict
            elif isinstance(data, list):
                return [transform_to_wire(item, param_defs) for item in data]
            return data

        def process(data: Any, param_defs: Dict[str, ApiParameter], prefix: str = ""):
            if isinstance(data, dict):
                for key, value in data.items():
                    param_def = None
                    for p_name, p_def in param_defs.items():
                        if p_name == key or p_def.tag_name == key or p_name.split('.')[-1] == key.replace("data.", ""):
                            param_def = p_def
                            break
                    if not param_def:
                        flattened[prefix + key.replace("data.", "")] = value
                        continue
                    target_key = get_clean_key(param_def)
                    if param_def.param_type == "RepeatList" and isinstance(value, list):
                        if use_json_for_complex:
                            wire_data = [transform_to_wire(item, param_def.sub_parameters) for item in value]
                            flattened[target_key] = json.dumps(wire_data, ensure_ascii=False)
                        else:
                            for i, item in enumerate(value, 1):
                                process(item, param_def.sub_parameters, f"{prefix}{target_key}.{i}.")
                    elif isinstance(value, (dict, list)):
                        if use_json_for_complex and not prefix:
                            wire_data = transform_to_wire(value, param_def.sub_parameters)
                            flattened[target_key] = json.dumps(wire_data, ensure_ascii=False)
                        else:
                            process(value, param_def.sub_parameters, f"{prefix}{target_key}.")
                    else:
                        flattened[prefix + target_key] = value
        process(input_params, api_def.parameters)
        return flattened

    def call_api(self, api_name: str, params: Dict[str, Any], custom_path: str = None, method: str = None, use_json: bool = True):
        api_def = self.apis[api_name]
        final_params = self.flatten_params(params, api_def, use_json_for_complex=use_json)
        final_params["Action"] = api_name
        if "RequestId" not in final_params:
            final_params["RequestId"] = str(uuid.uuid4())
        actual_path = custom_path or self.gateway_paths.get(api_name, self.base_path)
        url = f"{self.protocol}://{self.host}:{self.port}{actual_path}"
        method = (method or api_def.methods[0]).upper()
        prep = requests.models.PreparedRequest()
        prep.prepare_url(url, final_params)
        full_url = prep.url
        try:
            if method == "GET":
                resp = requests.get(url, params=final_params, verify=False, timeout=30)
            else:
                resp = requests.post(url, data=final_params, verify=False, timeout=30)
            return resp, final_params, full_url
        except Exception as e:
            return f"Error: {str(e)}", final_params, full_url

# --- GUI 界面类 ---

class ApiGui:
    def __init__(self, root, client: BastionHostApiClient):
        self.root = root
        self.client = client
        self.root.title("BastionHost API Test Tool (GUI)")
        self.root.geometry("1250x850")
        self.current_api_name = None
        self.config_file = "server_configs.json"
        self.server_configs = self.load_server_configs()
        config_frame = ttk.LabelFrame(root, text="基本配置")
        config_frame.pack(fill="x", padx=10, pady=5)
        row1 = ttk.Frame(config_frame)
        row1.pack(fill="x", padx=5, pady=2)
        ttk.Label(row1, text="配置列表:").pack(side="left", padx=2)
        self.config_cb = ttk.Combobox(row1, values=list(self.server_configs.keys()), width=15, state="readonly")
        self.config_cb.pack(side="left", padx=5)
        self.config_cb.bind("<<ComboboxSelected>>", self.on_config_selected)
        ttk.Label(row1, text="别名:").pack(side="left", padx=2)
        self.alias_ent = ttk.Entry(row1, width=12)
        self.alias_ent.pack(side="left", padx=5)
        ttk.Button(row1, text="保存", width=5, command=self.save_current_config).pack(side="left", padx=2)
        ttk.Button(row1, text="删除", width=5, command=self.delete_selected_config).pack(side="left", padx=2)
        ttk.Label(row1, text="Host:").pack(side="left", padx=(10, 2))
        self.host_ent = ttk.Entry(row1, width=20)
        self.host_ent.insert(0, client.host)
        self.host_ent.pack(side="left", padx=5)
        ttk.Label(row1, text="Port:").pack(side="left", padx=2)
        self.port_ent = ttk.Entry(row1, width=8)
        self.port_ent.insert(0, str(client.port))
        self.port_ent.pack(side="left", padx=5)
        ttk.Label(row1, text="Proto:").pack(side="left", padx=2)
        self.proto_cb = ttk.Combobox(row1, values=["http", "https"], width=8)
        self.proto_cb.set(client.protocol)
        self.proto_cb.pack(side="left", padx=5)
        ttk.Label(row1, text="并发:").pack(side="left", padx=2)
        self.concurrent_ent = ttk.Entry(row1, width=5)
        self.concurrent_ent.insert(0, "1")
        self.concurrent_ent.pack(side="left", padx=5)
        ttk.Label(row1, text="参数策略:").pack(side="left", padx=2)
        self.strategy_cb = ttk.Combobox(row1, values=["固定值", "随机值"], width=8, state="readonly")
        self.strategy_cb.set("固定值")
        self.strategy_cb.pack(side="left", padx=5)
        self.json_var = tk.BooleanVar(value=True)
        self.json_cb = ttk.Checkbutton(row1, text="JSON 模式", variable=self.json_var)
        self.json_cb.pack(side="left", padx=5)
        row2 = ttk.Frame(config_frame)
        row2.pack(fill="x", padx=5, pady=5)
        ttk.Label(row2, text="Path:").pack(side="left", padx=2)
        self.path_ent = ttk.Entry(row2)
        self.path_ent.pack(side="left", fill="x", expand=True, padx=5)
        main_paned = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        main_paned.pack(fill="both", expand=True, padx=10, pady=5)
        left_frame = ttk.Frame(main_paned)
        main_paned.add(left_frame, weight=1)
        ttk.Label(left_frame, text="搜索接口 (双击定位文件):").pack(anchor="w")
        search_frame = ttk.Frame(left_frame)
        search_frame.pack(fill="x", pady=2)
        self.search_var = tk.StringVar()
        self.search_var.trace("w", self.filter_apis)
        ttk.Entry(search_frame, textvariable=self.search_var).pack(side="left", fill="x", expand=True)
        check_btn_frame = ttk.Frame(left_frame)
        check_btn_frame.pack(fill="x")
        ttk.Button(check_btn_frame, text="全选当前", command=self.select_all_visible, width=10).pack(side="left", padx=2)
        ttk.Button(check_btn_frame, text="清空所有", command=self.clear_all_selected, width=10).pack(side="left", padx=2)
        tree_container = ttk.Frame(left_frame)
        tree_container.pack(fill="both", expand=True)
        self.api_tree = ttk.Treeview(tree_container, columns=("check", "name"), show="headings", selectmode="browse")
        self.api_tree.heading("check", text="√", anchor="center")
        self.api_tree.heading("name", text="接口名称", anchor="w")
        self.api_tree.column("check", width=35, stretch=False, anchor="center")
        self.api_tree.column("name", width=200, stretch=True)
        scrollbar = ttk.Scrollbar(tree_container, orient="vertical", command=self.api_tree.yview)
        self.api_tree.configure(yscrollcommand=scrollbar.set)
        self.api_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.api_tree.bind("<Button-1>", self.on_tree_click)
        self.api_tree.bind("<Double-Button-1>", self.on_api_double_click)
        self.selected_apis = set()
        self.bulk_results = []
        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=3)
        ttk.Label(right_frame, text="请求参数 (JSON):").pack(anchor="w")
        self.param_text = scrolledtext.ScrolledText(right_frame, height=12, font=("Consolas", 11))
        self.param_text.pack(fill="x", pady=5)
        self.param_text.insert("1.0", "{\n  \n}")
        btn_frame = ttk.Frame(right_frame)
        btn_frame.pack(fill="x")
        self.run_btn = ttk.Button(btn_frame, text="提交请求", command=self.run_task)
        self.run_btn.pack(side="left", padx=5)
        self.bulk_btn = ttk.Button(btn_frame, text="批量测试", command=self.run_bulk_test)
        self.bulk_btn.pack(side="left", padx=5)
        self.bulk_details_btn = ttk.Button(btn_frame, text="查看批量详情", command=self.show_bulk_details, state="disabled")
        self.bulk_details_btn.pack(side="left", padx=5)
        self.export_html_btn = ttk.Button(btn_frame, text="导出HTML报告", command=self.export_results_to_html, state="disabled")
        self.export_html_btn.pack(side="left", padx=5)
        ttk.Button(btn_frame, text="自动填充", command=self.auto_fill_params).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="重置模板", command=self.reset_param_template).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="清空日志", command=lambda: self.res_text.delete("1.0", tk.END)).pack(side="left")
        ttk.Button(btn_frame, text="退出程序", command=root.quit).pack(side="right")
        ttk.Label(right_frame, text="执行结果:").pack(anchor="w", pady=(10,0))
        self.res_text = scrolledtext.ScrolledText(right_frame, font=("Consolas", 11), bg="#f5f5f5")
        self.res_text.pack(fill="both", expand=True, pady=5)
        self.res_text.tag_config("success", foreground="#28a745")
        self.res_text.tag_config("warning", foreground="#ffc107")
        self.res_text.tag_config("error", foreground="#dc3545")
        self.res_text.tag_config("bold", font=("Consolas", 11, "bold"))
        self.all_apis = sorted(list(self.client.apis.keys()))
        self.visible_apis = []
        self.filter_apis()

    def filter_apis(self, *args):
        search_input = self.search_var.get().strip()
        search_terms = [t.lower() for t in re.split(r'[\s,]+', search_input) if t]
        for item in self.api_tree.get_children():
            self.api_tree.delete(item)
        self.visible_apis = []
        for api in self.all_apis:
            api_lower = api.lower()
            if not search_terms:
                match = True
            else:
                match = any(term in api_lower for term in search_terms)
            if match:
                self.visible_apis.append(api)
                status = "☑" if api in self.selected_apis else "☐"
                self.api_tree.insert("", "end", iid=api, values=(status, api))

    def on_tree_click(self, event):
        region = self.api_tree.identify_region(event.x, event.y)
        if region == "cell":
            column = self.api_tree.identify_column(event.x)
            item_id = self.api_tree.identify_row(event.y)
            if not item_id: return
            if column == "#1":
                if item_id in self.selected_apis:
                    self.selected_apis.remove(item_id)
                    self.api_tree.set(item_id, column="check", value="☐")
                else:
                    self.selected_apis.add(item_id)
                    self.api_tree.set(item_id, column="check", value="☑")
            else:
                self.load_api_to_editor(item_id)

    def select_all_visible(self):
        for api in self.visible_apis:
            self.selected_apis.add(api)
            if self.api_tree.exists(api):
                self.api_tree.set(api, column="check", value="☑")

    def clear_all_selected(self):
        self.selected_apis.clear()
        for item in self.api_tree.get_children():
            self.api_tree.set(item, column="check", value="☐")

    def load_api_to_editor(self, api_name):
        api_def = self.client.apis[api_name]
        gate_path = self.client.gateway_paths.get(api_name) or f"/openapi/bhost/{api_def.version}/{api_name}.json"
        self.path_ent.delete(0, tk.END)
        self.path_ent.insert(0, gate_path)
        if api_name != self.current_api_name:
            self.current_api_name = api_name
            self.reset_param_template()

    def reset_param_template(self):
        if not self.current_api_name: return
        api_def = self.client.apis[self.current_api_name]
        template = {}
        for p_name, p_def in api_def.parameters.items():
            if p_def.required and p_def.tag_position != "System":
                if p_def.param_type == "RepeatList":
                    template[p_name] = [{}]
                else:
                    template[p_name] = ""
        self.param_text.delete("1.0", tk.END)
        self.param_text.insert("1.0", json.dumps(template, indent=2, ensure_ascii=False))

    def on_api_double_click(self, event):
        item_id = self.api_tree.identify_row(event.y)
        if not item_id: return
        api_def = self.client.apis[item_id]
        try:
            if os.name == 'nt':
                subprocess.run(['explorer', '/select,', api_def.file_path])
            else:
                folder = os.path.dirname(api_def.file_path)
                subprocess.call(('open' if sys.platform == 'darwin' else 'xdg-open', folder))
        except Exception as e:
            messagebox.showerror("Error", f"无法定位文件: {e}")

    def log(self, msg, tag=None):
        self.res_text.insert(tk.END, str(msg) + "\n", tag)
        self.res_text.see(tk.END)

    def load_server_configs(self) -> Dict[str, Dict[str, Any]]:
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except: pass
        return {}

    def save_current_config(self):
        host = self.host_ent.get().strip()
        port = self.port_ent.get().strip()
        proto = self.proto_cb.get()
        alias = self.alias_ent.get().strip()
        if not host: return
        name = alias if alias else f"{proto}://{host}:{port}"
        self.server_configs[name] = {"host": host, "port": port, "proto": proto, "alias": alias}
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(self.server_configs, f, indent=2, ensure_ascii=False)
        self.config_cb["values"] = list(self.server_configs.keys())
        self.config_cb.set(name)
        messagebox.showinfo("Success", "配置已保存")

    def delete_selected_config(self):
        name = self.config_cb.get()
        if name in self.server_configs:
            del self.server_configs[name]
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.server_configs, f, indent=2, ensure_ascii=False)
            self.config_cb["values"] = list(self.server_configs.keys())
            self.config_cb.set("")
            self.alias_ent.delete(0, tk.END)
            messagebox.showinfo("Success", "配置已删除")

    def on_config_selected(self, event):
        name = self.config_cb.get()
        if name in self.server_configs:
            cfg = self.server_configs[name]
            self.host_ent.delete(0, tk.END)
            self.host_ent.insert(0, cfg["host"])
            self.port_ent.delete(0, tk.END)
            self.port_ent.insert(0, cfg["port"])
            self.proto_cb.set(cfg["proto"])
            self.alias_ent.delete(0, tk.END)
            self.alias_ent.insert(0, cfg.get("alias", ""))

    def generate_mock_params(self, api_def: ApiDefinition) -> Dict[str, Any]:
        """为接口生成默认参数"""
        strategy = self.strategy_cb.get()

        def get_value(param: ApiParameter, force_fill: bool = False):
            name_lower = param.name.lower()
            if param.param_type == "RepeatList":
                item = {}
                for sub_p_name, sub_p_def in param.sub_parameters.items():
                    item[sub_p_name] = get_value(sub_p_def, force_fill=True)
                if not item and "idset" in name_lower:
                    key = param.name.replace("Set", "").replace("Id", "") + "Id"
                    item[key] = 10
                return [item]
            if any(k in name_lower for k in ["ip", "address", "host"]):
                is_invalid_example = not param.example or re.search(r'^[xX.\[\]\s]+$', param.example)
                if is_invalid_example:
                    if strategy == "随机值" or force_fill:
                        return ".".join(str(random.randint(1, 254)) for _ in range(4))
                    return "10.0.0.1"
            if param.example and param.example not in ["xxx", "[]", "xxx.xxx.xxx.xxx"]:
                if param.param_type in ["Integer", "Long"]:
                    try: return int(param.example)
                    except: pass
                elif param.param_type in ["Float", "Double"]:
                    try: return float(param.example)
                    except: pass
                elif param.param_type == "Boolean":
                    return str(param.example).lower() == "true"
                return param.example
            if "regionid" in name_lower: return "cn-hangzhou"
            if "page" in name_lower: return 1
            if "pagesize" in name_lower: return 10
            enum_match = re.search(r'[\[\(]([\w\s,，/|]+)[\]\)]', param.description)
            if enum_match:
                enums = [e.strip() for e in re.split(r'[,，/|]', enum_match.group(1)) if e.strip()]
                if enums:
                    return random.choice(enums) if strategy == "随机值" else enums[0]
            
            if strategy == "随机值":
                if param.param_type in ["Integer", "Long"]:
                    return random.randint(1, 1000)
                elif param.param_type == "Boolean":
                    return random.choice([True, False])
                elif param.param_type in ["Float", "Double"]:
                    return round(random.uniform(1.0, 100.0), 2)
                else:
                    if "id" in name_lower: return random.randint(10, 999)
                    return "".join(random.choices(string.ascii_letters + string.digits, k=8))
            else:
                if param.param_type in ["Integer", "Long"]:
                    return 1
                elif param.param_type == "Boolean":
                    return True
                elif param.param_type in ["Float", "Double"]:
                    return 1.0
                else:
                    if "id" in name_lower: return 10
                    return "test_value"

        params = {}
        for p_name, p_def in api_def.parameters.items():
            if p_def.tag_position != "System":
                if p_def.required or p_def.param_type == "RepeatList":
                    params[p_name] = get_value(p_def)
        return params

    def auto_fill_params(self):
        if not self.current_api_name:
            messagebox.showwarning("Warning", "请在列表中选择一个接口")
            return
        api_def = self.client.apis[self.current_api_name]
        params = self.generate_mock_params(api_def)
        self.param_text.delete("1.0", tk.END)
        self.param_text.insert("1.0", json.dumps(params, indent=2, ensure_ascii=False))

    def run_bulk_test(self):
        if not self.selected_apis:
            messagebox.showwarning("Warning", "请在左侧勾选要测试的接口")
            return
        
        api_names = sorted(list(self.selected_apis))
        count = int(self.concurrent_ent.get())
        strategy = self.strategy_cb.get()
        
        self.run_btn.config(state="disabled")
        self.bulk_btn.config(state="disabled")
        self.bulk_details_btn.config(state="disabled")
        self.bulk_results = []
        
        def bulk_task_thread():
            self.log(f"--- 批量测试开始 (共 {len(api_names)} 个接口, 并发: {count}) ---", "bold")
            stats = {"total_req": 0, "success": 0, "fail": 0, "error": 0}
            self.client.host = self.host_ent.get()
            self.client.port = int(self.port_ent.get())
            self.client.protocol = self.proto_cb.get()
            
            for idx, api_name in enumerate(api_names, 1):
                api_def = self.client.apis[api_name]
                gate_path = self.client.gateway_paths.get(api_name) or f"/openapi/bhost/{api_def.version}/{api_name}.json"
                
                self.log(f"\n[正在测试 {idx}/{len(api_names)}: {api_name} (并发: {count})]")
                
                # 生成基础参数（用于并发请求的首个请求或固定策略）
                base_params = self.generate_mock_params(api_def)
                
                with ThreadPoolExecutor(max_workers=count) as executor:
                    futures = []
                    for i in range(1, count + 1):
                        current_p = base_params if (i == 1 or strategy == "固定值") else self.generate_mock_params(api_def)
                        futures.append(executor.submit(self.client.call_api, api_name, current_p, gate_path, use_json=self.json_var.get()))
                    
                    for i, future in enumerate(futures, 1):
                        stats["total_req"] += 1
                        result, actual_params, full_url = future.result()
                        
                        # 在名称后增加序号标记，以便在报告中区分
                        name_with_seq = f"{api_name} (#{i})" if count > 1 else api_name
                        res_info = {"name": name_with_seq, "url": full_url, "params": actual_params, "status": "Error", "response": "", "error": ""}
                        
                        if isinstance(result, str):
                            self.log(f"  Req #{i} 异常: {result}", "error")
                            res_info["error"] = result
                            stats["error"] += 1
                        else:
                            res_info["status"] = str(result.status_code)
                            if 200 <= result.status_code < 300:
                                self.log(f"  Req #{i} 成功: {result.status_code}", "success")
                                stats["success"] += 1
                            else:
                                self.log(f"  Req #{i} 失败: {result.status_code}", "warning")
                                stats["fail"] += 1
                            try:
                                resp_json = result.json()
                                res_info["response"] = resp_json
                            except:
                                res_info["response"] = result.text
                        self.bulk_results.append(res_info)
            
            self.log("\n" + "="*40)
            self.log("批量并发测试统计:", "bold")
            self.log(f"总请求数: {stats['total_req']}")
            self.log(f"成功: {stats['success']}", "success")
            self.log(f"失败: {stats['fail']}", "warning")
            self.log(f"异常: {stats['error']}", "error")
            self.log("="*40 + "\n")
            
            self.root.after(0, lambda: [
                self.run_btn.config(state="normal"), 
                self.bulk_btn.config(state="normal"),
                self.bulk_details_btn.config(state="normal"),
                self.export_html_btn.config(state="normal")
            ])
        threading.Thread(target=bulk_task_thread, daemon=True).start()

    def show_bulk_details(self):
        if not self.bulk_results:
            messagebox.showinfo("Info", "没有批量测试结果。")
            return
        detail_win = tk.Toplevel(self.root)
        detail_win.title("批量测试详情")
        detail_win.geometry("1100x750")
        paned = ttk.PanedWindow(detail_win, orient=tk.HORIZONTAL)
        paned.pack(fill="both", expand=True, padx=5, pady=5)
        left_frame = ttk.Frame(paned)
        paned.add(left_frame, weight=1)
        ttk.Label(left_frame, text="接口列表 (含并发序号):").pack(anchor="w")
        res_tree_container = ttk.Frame(left_frame)
        res_tree_container.pack(fill="both", expand=True)
        res_tree = ttk.Treeview(res_tree_container, columns=("status", "name"), show="headings", selectmode="browse")
        res_tree.heading("status", text="状态", anchor="center")
        res_tree.heading("name", text="接口名称", anchor="w")
        res_tree.column("status", width=60, stretch=False, anchor="center")
        res_tree.column("name", width=180, stretch=True)
        res_vsb = ttk.Scrollbar(res_tree_container, orient="vertical", command=res_tree.yview)
        res_tree.configure(yscrollcommand=res_vsb.set)
        res_tree.pack(side="left", fill="both", expand=True)
        res_vsb.pack(side="right", fill="y")
        right_frame = ttk.Frame(paned)
        paned.add(right_frame, weight=3)
        ttk.Label(right_frame, text="请求与响应详情:").pack(anchor="w")
        detail_text = scrolledtext.ScrolledText(right_frame, font=("Consolas", 10))
        detail_text.pack(fill="both", expand=True)
        def on_res_select(event):
            selected = res_tree.selection()
            if not selected: return
            idx = int(selected[0])
            res = self.bulk_results[idx]
            detail_text.delete("1.0", tk.END)
            detail_text.insert(tk.END, f"API名称: {res['name']}\n")
            detail_text.insert(tk.END, f"响应状态: {res['status']}\n")
            detail_text.insert(tk.END, f"请求URL: {res['url']}\n")
            detail_text.insert(tk.END, "\n--- 实际打平参数 ---\n")
            detail_text.insert(tk.END, json.dumps(res['params'], indent=2, ensure_ascii=False))
            detail_text.insert(tk.END, "\n\n--- 响应内容 ---\n")
            if res['error']:
                detail_text.insert(tk.END, f"Error: {res['error']}\n")
            else:
                try: detail_text.insert(tk.END, json.dumps(res['response'], indent=2, ensure_ascii=False))
                except: detail_text.insert(tk.END, str(res['response']))
        res_tree.bind("<<TreeviewSelect>>", on_res_select)
        for i, res in enumerate(self.bulk_results):
            res_tree.insert("", "end", iid=str(i), values=(res['status'], res['name']))
        if self.bulk_results: res_tree.selection_set("0")

    def export_results_to_html(self):
        if not self.bulk_results:
            messagebox.showinfo("Info", "没有测试结果可以导出。")
            return
        default_filename = f"api_test_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        file_path = filedialog.asksaveasfilename(
            defaultextension=".html",
            filetypes=[("HTML files", "*.html"), ("All files", "*.*")],
            initialfile=default_filename,
            title="选择导出路径"
        )
        if not file_path: return
        html_template = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>BastionHost API 并发测试报告</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f8f9fa; color: #333; margin: 20px; }}
        .container {{ max-width: 1350px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        h1 {{ color: #007bff; border-bottom: 2px solid #eee; padding-bottom: 10px; margin-bottom: 20px; }}
        .summary {{ display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 20px; padding: 15px; background: #e9ecef; border-radius: 5px; }}
        .summary-item {{ font-weight: bold; font-size: 14px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; table-layout: fixed; }}
        th, td {{ padding: 12px; border: 1px solid #dee2e6; text-align: left; vertical-align: top; overflow-wrap: break-word; font-size: 13px; }}
        th {{ background-color: #007bff; color: white; position: sticky; top: 0; }}
        tr:nth-child(even) {{ background-color: #f8f9fc; }}
        tr:hover {{ background-color: #f1f4f9; }}
        .status-success {{ color: #28a745; font-weight: bold; }}
        .status-fail {{ color: #ffc107; font-weight: bold; }}
        .status-error {{ color: #dc3545; font-weight: bold; }}
        pre {{ background: #272822; color: #f8f8f2; padding: 12px; border-radius: 4px; font-size: 12px; white-space: pre-wrap; margin: 0; max-height: 500px; overflow-y: auto; font-family: 'Consolas', monospace; }}
        .section-title {{ font-weight: bold; margin-bottom: 5px; color: #555; display: block; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>BastionHost API 并发测试报告</h1>
        <div class="summary">
            <div class="summary-item">报告时间: {now}</div>
            <div class="summary-item">总请求数: {total}</div>
            <div class="summary-item">成功: <span class="status-success">{success}</span></div>
            <div class="summary-item">失败: <span class="status-fail">{fail}</span></div>
            <div class="summary-item">异常: <span class="status-error">{error}</span></div>
        </div>
        <table>
            <tr>
                <th style="width: 15%;">接口名称 (序号)</th>
                <th style="width: 8%;">状态码</th>
                <th style="width: 35%;">请求详情</th>
                <th style="width: 42%;">响应内容</th>
            </tr>
            {rows}
        </table>
    </div>
</body>
</html>
"""
        rows_html = ""
        success_count = fail_count = error_count = 0
        for i, res in enumerate(self.bulk_results):
            status_class = "status-success"
            if res['status'] == "Error":
                status_class = "status-error"; error_count += 1
            elif int(res['status']) >= 300:
                status_class = "status-fail"; fail_count += 1
            else: success_count += 1
            params_json = json.dumps(res['params'], indent=2, ensure_ascii=False)
            try: resp_json = json.dumps(res['response'], indent=2, ensure_ascii=False)
            except: resp_json = str(res['response'])
            row = f"""
            <tr>
                <td><strong>{res['name']}</strong></td>
                <td><span class="{status_class}">{res['status']}</span></td>
                <td>
                    <div style="font-size: 11px; color: #666; margin-bottom: 8px;">URL: {res['url']}</div>
                    <span class="section-title">打平参数:</span>
                    <pre>{params_json}</pre>
                </td>
                <td>
                    {f'<div class="status-error" style="margin-bottom:8px;">Error: {res["error"]}</div>' if res["error"] else ""}
                    <span class="section-title">响应数据:</span>
                    <pre>{resp_json}</pre>
                </td>
            </tr>
            """
            rows_html += row
        final_html = html_template.format(now=datetime.now().strftime('%Y-%m-%d %H:%M:%S'), total=len(self.bulk_results), success=success_count, fail=fail_count, error=error_count, rows=rows_html)
        try:
            with open(file_path, "w", encoding="utf-8") as f: f.write(final_html)
            if messagebox.askyesno("Success", f"报告已成功导出至:\n{file_path}\n\n是否立即在浏览器中打开？"):
                if os.name == 'nt': os.startfile(file_path)
                elif sys.platform == 'darwin': subprocess.call(('open', file_path))
                else: subprocess.call(('xdg-open', file_path))
        except Exception as e: messagebox.showerror("Error", f"导出失败: {e}")

    def run_task(self):
        if not self.current_api_name:
            messagebox.showwarning("Warning", "请在列表中选择一个接口")
            return
        api_name = self.current_api_name
        api_def = self.client.apis[api_name]
        strategy = self.strategy_cb.get()
        try: editor_params = json.loads(self.param_text.get("1.0", tk.END))
        except:
            if strategy == "固定值":
                messagebox.showerror("Error", "JSON 格式有误。")
                return
            editor_params = {}
        self.client.host = self.host_ent.get()
        self.client.port = int(self.port_ent.get())
        self.client.protocol = self.proto_cb.get()
        custom_path = self.path_ent.get()
        count = int(self.concurrent_ent.get())
        self.run_btn.config(state="disabled")
        self.bulk_btn.config(state="disabled")
        def task_thread():
            self.log(f"--- 任务开始: {api_name} (策略: {strategy}, 并发: {count}) ---", "bold")
            with ThreadPoolExecutor(max_workers=count) as executor:
                futures = []
                for i in range(1, count + 1):
                    current_p = editor_params if (i == 1 or strategy == "固定值") else self.generate_mock_params(api_def)
                    futures.append(executor.submit(self.client.call_api, api_name, current_p, custom_path, use_json=self.json_var.get()))
                for i, future in enumerate(futures, 1):
                    result, actual_params, full_url = future.result()
                    self.log(f"\n[请求 #{i}]")
                    self.log(f"浏览器 URL:\n{full_url}")
                    self.log(f"\n实际打平参数:\n{json.dumps(actual_params, indent=2, ensure_ascii=False)}")
                    if isinstance(result, str): self.log(f"\n请求失败: {result}", "error")
                    else:
                        tag = "success" if 200 <= result.status_code < 300 else "warning"
                        self.log(f"\n响应状态码: {result.status_code}", tag)
                        try: self.log(f"响应内容:\n{json.dumps(result.json(), indent=2, ensure_ascii=False)}")
                        except: self.log(f"响应内容:\n{result.text[:2000]}")
            self.log("\n--- 任务结束 ---", "bold")
            self.root.after(0, lambda: [self.run_btn.config(state="normal"), self.bulk_btn.config(state="normal")])
        threading.Thread(target=task_thread, daemon=True).start()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("api", nargs="?")
    parser.add_argument("-p", "--params", default="{}")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--protocol", default="http")
    parser.add_argument("--path", default="/")
    parser.add_argument("--pop-dir", default="pop/Apis/2020-02-18")
    parser.add_argument("--gate-dir", default="api")
    args = parser.parse_args()
    client = BastionHostApiClient(args.host, args.port, args.protocol, args.path)
    print(f"正在加载定义...")
    client.load_apis(args.pop_dir, args.gate_dir)
    print(f"成功加载接口定义。")
    if args.gui or (not args.api):
        root = tk.Tk()
        app = ApiGui(root, client)
        root.mainloop()
    else:
        try:
            param_str = args.params
            if param_str.startswith("@"):
                with open(param_str[1:], 'r', encoding='utf-8') as f: param_str = f.read()
            params = json.loads(param_str)
            resp, actual, full_url = client.call_api(args.api, params)
            print(f"URL: {full_url}")
            try: print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
            except: print(resp.text)
        except Exception as e: print(f"Error: {e}")

if __name__ == "__main__":
    main()

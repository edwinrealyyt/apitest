import os
import xml.etree.ElementTree as ET
import requests
import json
import argparse
import uuid
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

# 忽略 HTTPS 警告
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 核心逻辑类 ---

class ApiParameter:
    def __init__(self, name: str, tag_name: str, tag_position: str, param_type: str, required: bool):
        self.name = name
        self.tag_name = tag_name
        self.tag_position = tag_position
        self.param_type = param_type
        self.required = required
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
        # 存储从 api/ 目录解析出的实际请求路径
        self.gateway_paths: Dict[str, str] = {}

    def load_apis(self, pop_dir: str, gate_dir: str):
        """加载 POP 参数定义和网关路径定义"""
        # 1. 加载参数定义 (pop/Apis)
        for root, _, files in os.walk(pop_dir):
            for file in files:
                if file.endswith(".xml"):
                    self._parse_pop_xml(os.path.join(root, file))
        
        # 2. 加载实际路径定义 (api/)
        for root, _, files in os.walk(gate_dir):
            for file in files:
                if file.endswith(".xml"):
                    self._parse_gate_xml(os.path.join(root, file))

    def _parse_pop_xml(self, file_path: str):
        """解析 POP 格式 XML (参数)"""
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
        """解析网关格式 XML (路径)"""
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()
            # 兼容 <api> 根节点
            if root.tag.lower() != "api": return
            
            name_node = root.find("name")
            api_name = name_node.text if name_node is not None else None
            if not api_name: return

            # 查找 <property pattern="constant" mapping="path" value="..." />
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
            required = param_node.attrib.get("required", "false").lower() == "true"
            param = ApiParameter(name, tag_name, tag_position, param_type, required)
            sub_params_node = param_node.find("Parameters")
            if sub_params_node is not None:
                self._parse_parameters(sub_params_node, param.sub_parameters)
            target_dict[name] = param

    def flatten_params(self, input_params: Dict[str, Any], api_def: ApiDefinition) -> Dict[str, Any]:
        flattened = {}
        def process(data: Any, param_defs: Dict[str, ApiParameter], prefix: str = ""):
            if isinstance(data, dict):
                for key, value in data.items():
                    param_def = None
                    for p_name, p_def in param_defs.items():
                        if p_name == key or p_def.tag_name == key or p_name.split('.')[-1] == key:
                            param_def = p_def
                            break
                    if not param_def:
                        flattened[prefix + key] = value
                        continue
                    if param_def.param_type == "RepeatList" and isinstance(value, list):
                        for i, item in enumerate(value, 1):
                            process(item, param_def.sub_parameters, f"{prefix}{param_def.tag_name}.{i}.")
                    elif isinstance(value, (dict, list)):
                        process(value, param_def.sub_parameters, f"{prefix}{param_def.tag_name}.")
                    else:
                        flattened[prefix + param_def.tag_name] = value
        process(input_params, api_def.parameters)
        return flattened

    def call_api(self, api_name: str, params: Dict[str, Any], custom_path: str = None, method: str = None):
        api_def = self.apis[api_name]
        final_params = self.flatten_params(params, api_def)
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

        # --- Config ---
        config_frame = ttk.LabelFrame(root, text="基本配置")
        config_frame.pack(fill="x", padx=10, pady=5)
        
        row1 = ttk.Frame(config_frame)
        row1.pack(fill="x", padx=5, pady=2)
        
        ttk.Label(row1, text="Host:").pack(side="left", padx=2)
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

        row2 = ttk.Frame(config_frame)
        row2.pack(fill="x", padx=5, pady=5)
        
        ttk.Label(row2, text="Path:").pack(side="left", padx=2)
        self.path_ent = ttk.Entry(row2)
        self.path_ent.pack(side="left", fill="x", expand=True, padx=5)

        # --- Main ---
        main_paned = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        main_paned.pack(fill="both", expand=True, padx=10, pady=5)

        left_frame = ttk.Frame(main_paned)
        main_paned.add(left_frame, weight=1)

        ttk.Label(left_frame, text="搜索接口 (双击定位文件):").pack(anchor="w")
        self.search_var = tk.StringVar()
        self.search_var.trace("w", self.filter_apis)
        ttk.Entry(left_frame, textvariable=self.search_var).pack(fill="x", pady=2)

        self.api_listbox = tk.Listbox(left_frame, font=("Consolas", 10), exportselection=False)
        self.api_listbox.pack(fill="both", expand=True)
        self.api_listbox.bind("<<ListboxSelect>>", self.on_api_select)
        self.api_listbox.bind("<Double-Button-1>", self.on_api_double_click)
        
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
        ttk.Button(btn_frame, text="重置模板", command=self.reset_param_template).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="清空日志", command=lambda: self.res_text.delete("1.0", tk.END)).pack(side="left")
        ttk.Button(btn_frame, text="退出程序", command=root.quit).pack(side="right")

        ttk.Label(right_frame, text="执行结果:").pack(anchor="w", pady=(10,0))
        self.res_text = scrolledtext.ScrolledText(right_frame, font=("Consolas", 11), bg="#f5f5f5")
        self.res_text.pack(fill="both", expand=True, pady=5)

        self.all_apis = sorted(list(self.client.apis.keys()))
        self.filter_apis()

    def filter_apis(self, *args):
        search_term = self.search_var.get().lower()
        self.api_listbox.delete(0, tk.END)
        for api in self.all_apis:
            if search_term in api.lower():
                self.api_listbox.insert(tk.END, api)

    def on_api_select(self, event):
        selection = self.api_listbox.curselection()
        if not selection: return
        api_name = self.api_listbox.get(selection[0])
        api_def = self.client.apis[api_name]
        
        # 优先从网关配置中获取 Path
        gate_path = self.client.gateway_paths.get(api_name)
        if not gate_path:
            # 备选方案：如果网关目录没搜到，按常规规律构造
            gate_path = f"/openapi/bhost/{api_def.version}/{api_name}.json"
        
        self.path_ent.delete(0, tk.END)
        self.path_ent.insert(0, gate_path)
        
        if api_name != self.current_api_name:
            self.current_api_name = api_name
            self.reset_param_template()

    def reset_param_template(self):
        selection = self.api_listbox.curselection()
        if not selection: return
        api_name = self.api_listbox.get(selection[0])
        api_def = self.client.apis[api_name]
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
        selection = self.api_listbox.curselection()
        if not selection: return
        api_name = self.api_listbox.get(selection[0])
        api_def = self.client.apis[api_name]
        try:
            if os.name == 'nt':
                subprocess.run(['explorer', '/select,', api_def.file_path])
            else:
                folder = os.path.dirname(api_def.file_path)
                subprocess.call(('open' if sys.platform == 'darwin' else 'xdg-open', folder))
        except Exception as e:
            messagebox.showerror("Error", f"无法定位文件: {e}")

    def log(self, msg):
        self.res_text.insert(tk.END, str(msg) + "\n")
        self.res_text.see(tk.END)

    def run_task(self):
        selection = self.api_listbox.curselection()
        if not selection:
            messagebox.showwarning("Warning", "请选择接口")
            return
        api_name = self.api_listbox.get(selection[0])
        try:
            params = json.loads(self.param_text.get("1.0", tk.END))
        except:
            messagebox.showerror("Error", "JSON 格式有误。")
            return

        self.client.host = self.host_ent.get()
        self.client.port = int(self.port_ent.get())
        self.client.protocol = self.proto_cb.get()
        custom_path = self.path_ent.get()
        
        count = int(self.concurrent_ent.get())
        self.run_btn.config(state="disabled")
        
        def task_thread():
            self.log(f"--- 任务开始: {api_name} ---")
            with ThreadPoolExecutor(max_workers=count) as executor:
                futures = [executor.submit(self.client.call_api, api_name, params, custom_path) for _ in range(count)]
                for i, future in enumerate(futures, 1):
                    result, actual_params, full_url = future.result()
                    self.log(f"\n[请求 #{i}]")
                    self.log(f"浏览器 URL:\n{full_url}")
                    self.log(f"\n实际打平参数:\n{json.dumps(actual_params, indent=2, ensure_ascii=False)}")
                    
                    if isinstance(result, str):
                        self.log(f"\n请求失败: {result}")
                    else:
                        self.log(f"\n响应状态码: {result.status_code}")
                        try:
                            self.log(f"响应内容:\n{json.dumps(result.json(), indent=2, ensure_ascii=False)}")
                        except:
                            self.log(f"响应内容:\n{result.text[:2000]}")
            self.log("\n--- 任务结束 ---")
            self.root.after(0, lambda: self.run_btn.config(state="normal"))
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
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    main()

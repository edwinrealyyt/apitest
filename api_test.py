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

        ttk.Label(row1, text="参数策略:").pack(side="left", padx=2)
        self.strategy_cb = ttk.Combobox(row1, values=["固定值", "随机值"], width=8, state="readonly")
        self.strategy_cb.set("固定值")
        self.strategy_cb.pack(side="left", padx=5)

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
        
        search_frame = ttk.Frame(left_frame)
        search_frame.pack(fill="x", pady=2)
        
        self.search_var = tk.StringVar()
        self.search_var.trace("w", self.filter_apis)
        ttk.Entry(search_frame, textvariable=self.search_var).pack(side="left", fill="x", expand=True)
        
        # 批量勾选操作按钮
        check_btn_frame = ttk.Frame(left_frame)
        check_btn_frame.pack(fill="x")
        ttk.Button(check_btn_frame, text="全选当前", command=self.select_all_visible, width=10).pack(side="left", padx=2)
        ttk.Button(check_btn_frame, text="清空所有", command=self.clear_all_selected, width=10).pack(side="left", padx=2)

        # 使用 Treeview 替代 Listbox 以支持勾选框列
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
        
        # 用于记录跨搜索持久化的勾选状态
        self.selected_apis = set()
        self.bulk_results = [] # 存储批量测试的详细结果
        
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
        ttk.Button(btn_frame, text="重置模板", command=self.reset_param_template).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="清空日志", command=lambda: self.res_text.delete("1.0", tk.END)).pack(side="left")
        ttk.Button(btn_frame, text="退出程序", command=root.quit).pack(side="right")

        ttk.Label(right_frame, text="执行结果:").pack(anchor="w", pady=(10,0))
        self.res_text = scrolledtext.ScrolledText(right_frame, font=("Consolas", 11), bg="#f5f5f5")
        self.res_text.pack(fill="both", expand=True, pady=5)

        # 配置颜色标签
        self.res_text.tag_config("success", foreground="#28a745")
        self.res_text.tag_config("warning", foreground="#ffc107")
        self.res_text.tag_config("error", foreground="#dc3545")
        self.res_text.tag_config("bold", font=("Consolas", 11, "bold"))

        self.all_apis = sorted(list(self.client.apis.keys()))
        self.visible_apis = [] # 当前搜索可见的接口列表
        self.filter_apis()

    def filter_apis(self, *args):
        search_input = self.search_var.get().strip()
        # 使用正则表达式按空白字符或逗号分割搜索词
        search_terms = [t.lower() for t in re.split(r'[\s,]+', search_input) if t]
        
        # 清空当前视图
        for item in self.api_tree.get_children():
            self.api_tree.delete(item)
        
        self.visible_apis = []
        for api in self.all_apis:
            api_lower = api.lower()
            
            # 匹配逻辑：如果没有搜索词则显示所有；如果有，则接口名需包含其中任意一个词（OR 逻辑）
            if not search_terms:
                match = True
            else:
                match = any(term in api_lower for term in search_terms)
            
            if match:
                self.visible_apis.append(api)
                status = "☑" if api in self.selected_apis else "☐"
                self.api_tree.insert("", "end", iid=api, values=(status, api))

    def on_tree_click(self, event):
        """处理 Treeview 点击事件：点击勾选框列则切换状态，点击名称列则显示参数"""
        region = self.api_tree.identify_region(event.x, event.y)
        if region == "cell":
            column = self.api_tree.identify_column(event.x)
            item_id = self.api_tree.identify_row(event.y)
            if not item_id: return
            
            if column == "#1": # 勾选框列
                if item_id in self.selected_apis:
                    self.selected_apis.remove(item_id)
                    self.api_tree.set(item_id, column="check", value="☐")
                else:
                    self.selected_apis.add(item_id)
                    self.api_tree.set(item_id, column="check", value="☑")
            else: # 名称列
                self.load_api_to_editor(item_id)

    def select_all_visible(self):
        """全选当前搜索可见的所有接口"""
        for api in self.visible_apis:
            self.selected_apis.add(api)
            if self.api_tree.exists(api):
                self.api_tree.set(api, column="check", value="☑")

    def clear_all_selected(self):
        """清空所有已勾选的接口"""
        self.selected_apis.clear()
        for item in self.api_tree.get_children():
            self.api_tree.set(item, column="check", value="☐")

    def load_api_to_editor(self, api_name):
        """加载接口到右侧编辑器"""
        api_def = self.client.apis[api_name]
        gate_path = self.client.gateway_paths.get(api_name) or f"/openapi/bhost/{api_def.version}/{api_name}.json"
        
        self.path_ent.delete(0, tk.END)
        self.path_ent.insert(0, gate_path)
        
        if api_name != self.current_api_name:
            self.current_api_name = api_name
            self.reset_param_template()

    def on_api_select(self, event):
        # Treeview 已经通过 on_tree_click 接管了单选逻辑，此方法保留为空或移除
        pass

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

    def generate_mock_params(self, api_def: ApiDefinition) -> Dict[str, Any]:
        """为接口生成默认参数"""
        strategy = self.strategy_cb.get()

        def get_value(param: ApiParameter):
            name_lower = param.name.lower()
            
            # 特殊字段处理
            if "regionid" in name_lower: return "cn-hangzhou"
            if "page" in name_lower: return 1
            if "pagesize" in name_lower: return 10
            
            if param.param_type == "RepeatList":
                item = {}
                for sub_p_name, sub_p_def in param.sub_parameters.items():
                    item[sub_p_name] = get_value(sub_p_def)
                return [item]
            
            # 根据策略生成
            if strategy == "随机值":
                if param.param_type in ["Integer", "Long"]:
                    return random.randint(1, 1000)
                elif param.param_type == "Boolean":
                    return random.choice([True, False])
                elif param.param_type in ["Float", "Double"]:
                    return round(random.uniform(1.0, 100.0), 2)
                else:
                    # 随机 8 位字符串
                    return "".join(random.choices(string.ascii_letters + string.digits, k=8))
            else:
                # 固定值策略
                if param.param_type in ["Integer", "Long"]:
                    return 1
                elif param.param_type == "Boolean":
                    return True
                elif param.param_type in ["Float", "Double"]:
                    return 1.0
                else:
                    return "test_value"

        params = {}
        for p_name, p_def in api_def.parameters.items():
            if p_def.required and p_def.tag_position != "System":
                params[p_name] = get_value(p_def)
        return params

    def run_bulk_test(self):
        if not self.selected_apis:
            messagebox.showwarning("Warning", "请在左侧勾选要测试的接口")
            return
        
        api_names = sorted(list(self.selected_apis))
        self.run_btn.config(state="disabled")
        self.bulk_btn.config(state="disabled")
        self.bulk_details_btn.config(state="disabled")
        self.bulk_results = [] # 重置结果

        def bulk_task_thread():
            self.log(f"--- 批量测试开始 (共 {len(api_names)} 个已勾选接口) ---", "bold")
            stats = {"total": 0, "success": 0, "fail": 0, "error": 0}
            
            # 使用用户设置的 host/port/proto
            self.client.host = self.host_ent.get()
            self.client.port = int(self.port_ent.get())
            self.client.protocol = self.proto_cb.get()

            for api_name in api_names:
                stats["total"] += 1
                api_def = self.client.apis[api_name]
                params = self.generate_mock_params(api_def)
                
                gate_path = self.client.gateway_paths.get(api_name) or f"/openapi/bhost/{api_def.version}/{api_name}.json"
                
                self.log(f"\n[测试接口 {stats['total']}/{len(api_names)}: {api_name}]")
                result, actual_params, full_url = self.client.call_api(api_name, params, gate_path)
                
                res_info = {
                    "name": api_name,
                    "url": full_url,
                    "params": actual_params,
                    "status": "Error",
                    "response": "",
                    "error": ""
                }

                if isinstance(result, str):
                    self.log(f"请求异常: {result}", "error")
                    res_info["error"] = result
                    stats["error"] += 1
                else:
                    res_info["status"] = str(result.status_code)
                    if 200 <= result.status_code < 300:
                        self.log(f"响应成功: {result.status_code}", "success")
                        stats["success"] += 1
                    else:
                        self.log(f"响应失败: {result.status_code}", "warning")
                        stats["fail"] += 1
                    try:
                        resp_json = result.json()
                        res_info["response"] = resp_json
                        self.log(f"内容摘要: {json.dumps(resp_json, ensure_ascii=False)[:200]}...")
                    except:
                        res_info["response"] = result.text
                        self.log(f"内容摘要: {result.text[:200]}...")
                
                self.bulk_results.append(res_info)
            
            # 统计汇总
            self.log("\n" + "="*40)
            self.log("批量测试结果统计:", "bold")
            self.log(f"总测试数: {stats['total']}")
            self.log(f"成功次数: {stats['success']}", "success")
            self.log(f"失败次数: {stats['fail']}", "warning")
            self.log(f"异常次数: {stats['error']}", "error")
            self.log("="*40 + "\n")
            
            self.root.after(0, lambda: [
                self.run_btn.config(state="normal"), 
                self.bulk_btn.config(state="normal"),
                self.bulk_details_btn.config(state="normal")
            ])
            
        threading.Thread(target=bulk_task_thread, daemon=True).start()

    def show_bulk_details(self):
        """显示批量测试结果详情界面"""
        if not self.bulk_results:
            messagebox.showinfo("Info", "没有批量测试结果。")
            return
            
        detail_win = tk.Toplevel(self.root)
        detail_win.title("批量测试详情")
        detail_win.geometry("1100x750")
        
        # 使用 PanedWindow 左右布局
        paned = ttk.PanedWindow(detail_win, orient=tk.HORIZONTAL)
        paned.pack(fill="both", expand=True, padx=5, pady=5)
        
        left_frame = ttk.Frame(paned)
        paned.add(left_frame, weight=1)
        
        ttk.Label(left_frame, text="接口列表:").pack(anchor="w")
        
        # 结果列表
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
                try:
                    detail_text.insert(tk.END, json.dumps(res['response'], indent=2, ensure_ascii=False))
                except:
                    detail_text.insert(tk.END, str(res['response']))

        res_tree.bind("<<TreeviewSelect>>", on_res_select)
        
        # 填充列表
        for i, res in enumerate(self.bulk_results):
            res_tree.insert("", "end", iid=str(i), values=(res['status'], res['name']))
        
        if self.bulk_results:
            res_tree.selection_set("0")

    def run_task(self):
        if not self.current_api_name:
            messagebox.showwarning("Warning", "请在列表中选择一个接口")
            return
        
        api_name = self.current_api_name
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
        self.bulk_btn.config(state="disabled")
        
        def task_thread():
            self.log(f"--- 任务开始: {api_name} ---", "bold")
            with ThreadPoolExecutor(max_workers=count) as executor:
                futures = [executor.submit(self.client.call_api, api_name, params, custom_path) for _ in range(count)]
                for i, future in enumerate(futures, 1):
                    result, actual_params, full_url = future.result()
                    self.log(f"\n[请求 #{i}]")
                    self.log(f"浏览器 URL:\n{full_url}")
                    self.log(f"\n实际打平参数:\n{json.dumps(actual_params, indent=2, ensure_ascii=False)}")
                    
                    if isinstance(result, str):
                        self.log(f"\n请求失败: {result}", "error")
                    else:
                        tag = "success" if 200 <= result.status_code < 300 else "warning"
                        self.log(f"\n响应状态码: {result.status_code}", tag)
                        try:
                            self.log(f"响应内容:\n{json.dumps(result.json(), indent=2, ensure_ascii=False)}")
                        except:
                            self.log(f"响应内容:\n{result.text[:2000]}")
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
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    main()

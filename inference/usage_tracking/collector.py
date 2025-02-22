import asyncio
import atexit
import hashlib
import json
import mimetypes
import socket
import sys
import time
from collections import defaultdict
from functools import wraps
from queue import Queue
from threading import Event, Lock, Thread
from typing import Any, Callable, DefaultDict, Dict, List, Optional, Tuple, Union
from uuid import uuid4

import requests

from inference.core.env import API_KEY, LAMBDA
from inference.core.logger import logger
from inference.core.version import __version__ as inference_version
from inference.core.workflows.execution_engine.compiler.entities import CompiledWorkflow
from inference.usage_tracking.utils import collect_func_params

from .config import TelemetrySettings, get_telemetry_settings

ResourceID = str
Usage = Union[DefaultDict[str, Any], Dict[str, Any]]
ResourceUsage = Union[DefaultDict[ResourceID, Usage], Dict[ResourceID, Usage]]
APIKey = str
APIKeyUsage = Union[DefaultDict[APIKey, ResourceUsage], Dict[APIKey, ResourceUsage]]
ResourceDetails = Dict[str, Any]
SystemDetails = Dict[str, Any]
UsagePayload = Union[APIKeyUsage, ResourceDetails, SystemDetails]


class UsageCollector:
    _lock = Lock()

    def __new__(cls, *args, **kwargs):
        with UsageCollector._lock:
            if not hasattr(cls, "_instance"):
                cls._instance = super().__new__(cls)
                cls._instance._queue = None
        return cls._instance

    def __init__(self):
        with UsageCollector._lock:
            if self._queue:
                return

        # Async lock only for async protection, should not be shared between threads
        self._async_lock = None
        try:
            self._async_lock = asyncio.Lock()
        except Exception as exc:
            logger.debug("Failed to create async lock %s", exc)

        self._exec_session_id = f"{time.time_ns()}_{uuid4().hex[:4]}"

        self._settings: TelemetrySettings = get_telemetry_settings()
        self._usage: APIKeyUsage = self.empty_usage_dict(
            exec_session_id=self._exec_session_id
        )

        # TODO: use persistent queue, i.e. https://pypi.org/project/persist-queue/
        self._queue: "Queue[UsagePayload]" = Queue(maxsize=self._settings.queue_size)
        self._queue_lock = Lock()

        self._system_info_sent: bool = False
        self._resource_details_lock = Lock()
        self._resource_details: DefaultDict[APIKey, Dict[ResourceID, bool]] = (
            defaultdict(dict)
        )

        self._terminate_collector_thread = Event()
        self._collector_thread = Thread(target=self._usage_collector, daemon=True)
        self._collector_thread.start()

        self._terminate_sender_thread = Event()
        self._sender_thread = Thread(target=self._usage_sender, daemon=True)
        self._sender_thread.start()

        atexit.register(self._cleanup)

    @staticmethod
    def empty_usage_dict(exec_session_id: str) -> APIKeyUsage:
        return defaultdict(  # api_key
            lambda: defaultdict(  # category:resource_id
                lambda: {
                    "timestamp_start": None,
                    "timestamp_stop": None,
                    "exec_session_id": exec_session_id,
                    "processed_frames": 0,
                    "fps": 0,
                    "source_duration": 0,
                    "category": "",
                    "resource_id": "",
                    "hosted": LAMBDA,
                    "api_key": None,
                    "enterprise": False,
                }
            )
        )

    @staticmethod
    def _merge_usage_dicts(d1: UsagePayload, d2: UsagePayload):
        merged = {}
        if d1 and d2 and d1.get("resource_id") != d2.get("resource_id"):
            raise ValueError("Cannot merge usage for different resource IDs")
        if "timestamp_start" in d1 and "timestamp_start" in d2:
            merged["timestamp_start"] = min(
                d1["timestamp_start"], d2["timestamp_start"]
            )
        if "timestamp_stop" in d1 and "timestamp_stop" in d2:
            merged["timestamp_stop"] = max(d1["timestamp_stop"], d2["timestamp_stop"])
        if "processed_frames" in d1 and "processed_frames" in d2:
            merged["processed_frames"] = d1["processed_frames"] + d2["processed_frames"]
        if "source_duration" in d1 and "source_duration" in d2:
            merged["source_duration"] = d1["source_duration"] + d2["source_duration"]
        return {**d1, **d2, **merged}

    def _dump_usage_queue_no_lock(self) -> List[APIKeyUsage]:
        usage_payloads: List[APIKeyUsage] = []
        while self._queue:
            if self._queue.empty():
                break
            usage_payloads.append(self._queue.get_nowait())
        return usage_payloads

    def _dump_usage_queue_with_lock(self) -> List[APIKeyUsage]:
        with self._queue_lock:
            usage_payloads = self._dump_usage_queue_no_lock()
        return usage_payloads

    @staticmethod
    def _get_api_key_usage_containing_resource(
        api_key: APIKey, usage_payloads: List[APIKeyUsage]
    ) -> Optional[ResourceUsage]:
        for usage_payload in usage_payloads:
            for other_api_key, resource_payloads in usage_payload.items():
                if api_key and other_api_key != api_key:
                    continue
                if other_api_key is None:
                    continue
                for resource_id, resource_usage in resource_payloads.items():
                    if not resource_id:
                        continue
                    if not resource_usage or "resource_id" not in resource_usage:
                        continue
                    return resource_usage
        return None

    @staticmethod
    def _zip_usage_payloads(usage_payloads: List[APIKeyUsage]) -> List[APIKeyUsage]:
        merged_api_key_usage_payloads: APIKeyUsage = {}
        system_info_payload = None
        for usage_payload in usage_payloads:
            for api_key, resource_payloads in usage_payload.items():
                if api_key is None:
                    if (
                        resource_payloads
                        and len(resource_payloads) > 1
                        or list(resource_payloads.keys()) != [None]
                    ):
                        logger.debug(
                            "Dropping usage payload %s due to missing API key",
                            resource_payloads,
                        )
                        continue
                    api_key_usage_with_resource = (
                        UsageCollector._get_api_key_usage_containing_resource(
                            api_key=api_key,
                            usage_payloads=usage_payloads,
                        )
                    )
                    if not api_key_usage_with_resource:
                        system_info_payload = resource_payloads
                        continue
                    api_key = api_key_usage_with_resource["api_key"]
                    resource_id = api_key_usage_with_resource["resource_id"]
                    category = api_key_usage_with_resource.get("category")
                    for v in resource_payloads.values():
                        v["api_key"] = api_key
                        if "resource_id" not in v or not v["resource_id"]:
                            v["resource_id"] = resource_id
                        if "category" not in v or not v["category"]:
                            v["category"] = category
                for (
                    resource_usage_key,
                    resource_usage_payload,
                ) in resource_payloads.items():
                    if resource_usage_key is None:
                        api_key_usage_with_resource = (
                            UsageCollector._get_api_key_usage_containing_resource(
                                api_key=api_key,
                                usage_payloads=usage_payloads,
                            )
                        )
                        if not api_key_usage_with_resource:
                            system_info_payload = {None: resource_usage_payload}
                            continue
                        resource_id = api_key_usage_with_resource["resource_id"]
                        category = api_key_usage_with_resource.get("category")
                        resource_usage_key = f"{category}:{resource_id}"
                        resource_usage_payload["api_key"] = api_key
                        resource_usage_payload["resource_id"] = resource_id
                        resource_usage_payload["category"] = category
                    merged_api_key_payload = merged_api_key_usage_payloads.setdefault(
                        api_key, {}
                    )
                    merged_resource_payload = merged_api_key_payload.setdefault(
                        resource_usage_key, {}
                    )
                    merged_api_key_payload[resource_usage_key] = (
                        UsageCollector._merge_usage_dicts(
                            merged_resource_payload,
                            resource_usage_payload,
                        )
                    )

        zipped_payloads = [merged_api_key_usage_payloads]
        if system_info_payload:
            system_info_api_key = next(iter(system_info_payload.values()))["api_key"]
            zipped_payloads.append({system_info_api_key: system_info_payload})
        return zipped_payloads

    @staticmethod
    def _hash(payload: str, length=5):
        payload_hash = hashlib.sha256(payload.encode())
        return payload_hash.hexdigest()[:length]

    def _enqueue_payload(self, payload: UsagePayload):
        logger.debug("Enqueuing usage payload %s", payload)
        if not payload:
            return
        with self._queue_lock:
            if not self._queue.full():
                self._queue.put(payload)
            else:
                usage_payloads = self._dump_usage_queue_no_lock()
                usage_payloads.append(payload)
                merged_usage_payloads = self._zip_usage_payloads(
                    usage_payloads=usage_payloads,
                )
                for usage_payload in merged_usage_payloads:
                    self._queue.put(usage_payload)

    @staticmethod
    def _calculate_resource_hash(resource_details: Dict[str, Any]) -> str:
        return UsageCollector._hash(json.dumps(resource_details, sort_keys=True))

    def record_resource_details(
        self,
        category: str,
        resource_details: Dict[str, Any],
        resource_id: Optional[str] = None,
        api_key: Optional[str] = None,
        enterprise: bool = False,
    ):
        if not category:
            raise ValueError("Category is compulsory when recording resource details.")
        if not resource_details and not resource_id:
            return
        if not isinstance(resource_details, dict) and not resource_id:
            return

        if not api_key:
            api_key = API_KEY
        if not resource_id:
            resource_id = UsageCollector._calculate_resource_hash(
                resource_details=resource_details
            )

        with self._resource_details_lock:
            api_key_specifications = self._resource_details[api_key]
            if resource_id in api_key_specifications:
                return
            api_key_specifications[resource_id] = True

        resource_details_payload: ResourceDetails = {
            api_key: {
                f"{category}:{resource_id}": {
                    "timestamp_start": time.time_ns(),
                    "category": category,
                    "resource_id": resource_id,
                    "hosted": LAMBDA,
                    "resource_details": json.dumps(resource_details),
                    "api_key": api_key,
                    "enterprise": enterprise,
                }
            }
        }
        logger.debug("Usage (%s details): %s", category, resource_details_payload)
        self._enqueue_payload(payload=resource_details_payload)

    @staticmethod
    def system_info(
        exec_session_id: str,
        api_key: Optional[str] = None,
        ip_address: Optional[str] = None,
        time_ns: Optional[int] = None,
        enterprise: bool = False,
    ) -> SystemDetails:
        if ip_address:
            ip_address_hash_hex = UsageCollector._hash(ip_address)
        else:
            try:
                ip_address: str = socket.gethostbyname(socket.gethostname())
            except:
                s = None
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    ip_address = s.getsockname()[0]
                except:
                    ip_address: str = socket.gethostbyname("localhost")

                if s:
                    s.close()

            ip_address_hash_hex = UsageCollector._hash(ip_address)

        if not time_ns:
            time_ns = time.time_ns()

        if not api_key:
            api_key = API_KEY

        return {
            "timestamp_start": time_ns,
            "exec_session_id": exec_session_id,
            "ip_address_hash": ip_address_hash_hex,
            "api_key": api_key,
            "hosted": LAMBDA,
            "is_gpu_available": False,  # TODO
            "python_version": sys.version.split()[0],
            "inference_version": inference_version,
            "enterprise": enterprise,
        }

    def record_system_info(
        self,
        api_key: str,
        ip_address: Optional[str] = None,
        enterprise: bool = False,
    ):
        if self._system_info_sent:
            return
        if not api_key:
            api_key = API_KEY
        system_info_payload = {
            api_key: {
                None: self.system_info(
                    exec_session_id=self._exec_session_id,
                    api_key=api_key,
                    ip_address=ip_address,
                    enterprise=enterprise,
                )
            }
        }
        logger.debug("Usage (system info): %s", system_info_payload)
        self._enqueue_payload(payload=system_info_payload)
        self._system_info_sent = True

    @staticmethod
    def _guess_source_type(source: str) -> str:
        mime_type, _ = mimetypes.guess_type(source)
        stream_schemes = ["rtsp", "rtmp"]
        source_type = None
        if mime_type and mime_type.startswith("video"):
            source_type = "video"
        elif mime_type and mime_type.startswith("image"):
            source_type = "image"
        elif mime_type:
            logger.debug("Unhandled mime type")
            source_type = mime_type.split("/")[0]
        elif not mime_type and str.isnumeric(source):
            source_type = "camera"
        elif not mime_type and any(
            source.lower().startswith(s) for s in stream_schemes
        ):
            source_type = "stream"
        return source_type

    def _update_usage_payload(
        self,
        source: str,
        category: str,
        frames: int = 1,
        api_key: Optional[str] = None,
        resource_details: Optional[Dict[str, Any]] = None,
        resource_id: Optional[str] = None,
        fps: float = 0,
        enterprise: bool = False,
    ):
        source = str(source) if source else ""
        if not api_key:
            api_key = API_KEY
        if not resource_id and resource_details:
            resource_id = UsageCollector._calculate_resource_hash(resource_details)
        with UsageCollector._lock:
            source_usage = self._usage[api_key][f"{category}:{resource_id}"]
            if not source_usage["timestamp_start"]:
                source_usage["timestamp_start"] = time.time_ns()
            source_usage["timestamp_stop"] = time.time_ns()
            source_usage["processed_frames"] += frames
            source_usage["fps"] = round(fps, 2)
            source_usage["source_duration"] += frames / fps if fps else 0
            source_usage["category"] = category
            source_usage["resource_id"] = resource_id
            source_usage["api_key"] = api_key
            source_usage["enterprise"] = enterprise
            logger.debug("Updated usage: %s", source_usage)

    def record_usage(
        self,
        source: str,
        category: str,
        enterprise: bool,
        frames: int = 1,
        api_key: Optional[str] = None,
        resource_details: Optional[Dict[str, Any]] = None,
        resource_id: Optional[str] = None,
        fps: float = 0,
    ) -> DefaultDict[str, Any]:
        if self._settings.opt_out and not enterprise:
            return
        self.record_system_info(
            api_key=api_key,
            enterprise=enterprise,
        )
        self.record_resource_details(
            category=category,
            resource_details=resource_details,
            resource_id=resource_id,
            api_key=api_key,
            enterprise=enterprise,
        )
        self._update_usage_payload(
            source=source,
            category=category,
            frames=frames,
            api_key=api_key,
            resource_details=resource_details,
            resource_id=resource_id,
            fps=fps,
            enterprise=enterprise,
        )

    async def async_record_usage(
        self,
        source: str,
        category: str,
        enterprise: bool,
        frames: int = 1,
        api_key: Optional[str] = None,
        resource_details: Optional[Dict[str, Any]] = None,
        resource_id: Optional[str] = None,
        fps: float = 0,
    ) -> DefaultDict[str, Any]:
        if self._async_lock:
            async with self._async_lock:
                self.record_usage(
                    source=source,
                    category=category,
                    frames=frames,
                    enterprise=enterprise,
                    api_key=api_key,
                    resource_details=resource_details,
                    resource_id=resource_id,
                    fps=fps,
                )
        else:
            self.record_usage(
                source=source,
                category=category,
                frames=frames,
                enterprise=enterprise,
                api_key=api_key,
                resource_details=resource_details,
                resource_id=resource_id,
                fps=fps,
            )

    def _usage_collector(self):
        while True:
            if self._terminate_collector_thread.wait(self._settings.flush_interval):
                break
            self._enqueue_usage_payload()
        logger.debug("Terminating collector thread")
        self._enqueue_usage_payload()

    def _enqueue_usage_payload(self):
        if not self._usage:
            return
        with UsageCollector._lock:
            self._enqueue_payload(payload=self._usage)
            self._usage = self.empty_usage_dict(exec_session_id=self._exec_session_id)

    def _usage_sender(self):
        while True:
            if self._terminate_sender_thread.wait(self._settings.flush_interval):
                break
            self._flush_queue()
        logger.debug("Terminating sender thread")
        self._flush_queue()

    def _flush_queue(self):
        usage_payloads = self._dump_usage_queue_with_lock()
        if not usage_payloads:
            return
        merged_payloads: APIKeyUsage = self._zip_usage_payloads(
            usage_payloads=usage_payloads,
        )
        self._offload_to_api(payloads=merged_payloads)

    def _offload_to_api(self, payloads: List[APIKeyUsage]):
        ssl_verify = True
        if "localhost" in self._settings.api_usage_endpoint_url.lower():
            ssl_verify = False
        if "127.0.0.1" in self._settings.api_usage_endpoint_url.lower():
            ssl_verify = False

        api_keys_failed = set()
        for payload in payloads:
            for api_key, workflow_payloads in payload.items():
                if any("processed_frames" not in w for w in workflow_payloads.values()):
                    api_keys_failed.add(api_key)
                    continue
                enterprise = any(
                    w.get("enterprise") for w in workflow_payloads.values()
                )
                try:
                    logger.debug(
                        "Offloading usage to %s, payload: %s",
                        self._settings.api_usage_endpoint_url,
                        workflow_payloads,
                    )
                    response = requests.post(
                        self._settings.api_usage_endpoint_url,
                        json=list(workflow_payloads.values()),
                        verify=ssl_verify,
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=1,
                    )
                except Exception as exc:
                    logger.debug("Failed to send usage - %s", exc)
                    api_keys_failed.add(api_key)
                    continue
                if response.status_code != 200:
                    logger.debug(
                        "Failed to send usage - got %s status code (%s)",
                        response.status_code,
                        response.raw,
                    )
                    api_keys_failed.add(api_key)
                    continue
            for api_key in list(payload.keys()):
                if api_key not in api_keys_failed:
                    del payload[api_key]
            if payload:
                logger.debug("Enqueuing back unsent payload")
                self._enqueue_payload(payload=payload)

    def push_usage_payloads(self):
        self._enqueue_usage_payload()
        self._flush_queue()

    async def async_push_usage_payloads(self):
        if self._async_lock:
            async with self._async_lock:
                self.push_usage_payloads()
        else:
            self.push_usage_payloads()

    @staticmethod
    def _resource_details_from_workflow_json(
        workflow_json: Dict[str, Any]
    ) -> Tuple[ResourceID, ResourceDetails]:
        if not isinstance(workflow_json, dict):
            raise ValueError("workflow_json must be dict")
        return {
            "steps": [
                f"{step.get('type', 'unknown')}:{step.get('name', 'unknown')}"
                for step in workflow_json.get("steps", [])
                if isinstance(step, dict)
            ]
        }

    @staticmethod
    def _extract_usage_params_from_func_kwargs(
        usage_fps: float,
        usage_api_key: str,
        usage_workflow_id: str,
        func: Callable[[Any], Any],
        args: List[Any],
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not usage_api_key:
            usage_api_key = API_KEY
        func_kwargs = collect_func_params(func, args, kwargs)
        resource_details = {}
        resource_id = None
        category = None
        if "workflow" in func_kwargs:
            workflow: CompiledWorkflow = func_kwargs["workflow"]
            if hasattr(workflow, "workflow_definition"):
                # TODO: handle enterprise blocks here
                workflow_definition = workflow.workflow_definition
                enterprise = False
            if hasattr(workflow, "init_parameters"):
                init_parameters = workflow.init_parameters
                if "workflows_core.api_key" in init_parameters:
                    usage_api_key = init_parameters["workflows_core.api_key"]
            workflow_json = {}
            if hasattr(workflow, "workflow_json"):
                workflow_json = workflow.workflow_json
            resource_details = UsageCollector._resource_details_from_workflow_json(
                workflow_json=workflow_json,
            )
            resource_id = usage_workflow_id
            if not resource_id and resource_details:
                usage_workflow_id = UsageCollector._calculate_resource_hash(
                    resource_details=resource_details
                )
            category = "workflows"
        elif "model_id" in func_kwargs:
            # TODO: handle model
            pass
        source = None
        runtime_parameters = func_kwargs.get("runtime_parameters")
        if (
            isinstance(runtime_parameters, dict)
            and "image" in func_kwargs["runtime_parameters"]
        ):
            images = runtime_parameters["image"]
            if not isinstance(images, list):
                images = [images]
            image = images[0]
            if isinstance(image, dict):
                source = image.get("value")
            elif hasattr(image, "_image_reference"):
                source = image._image_reference
        return {
            "source": source,
            "api_key": usage_api_key,
            "category": category,
            "resource_details": resource_details,
            "resource_id": resource_id,
            "fps": usage_fps,
            "enterprise": enterprise,
        }

    def __call__(self, func: Callable[[Any], Any]):
        @wraps(func)
        def sync_wrapper(
            *args,
            usage_fps: float = 0,
            usage_api_key: Optional[str] = None,
            usage_workflow_id: Optional[str] = None,
            **kwargs,
        ):
            self.record_usage(
                **self._extract_usage_params_from_func_kwargs(
                    usage_fps=usage_fps,
                    usage_api_key=usage_api_key,
                    usage_workflow_id=usage_workflow_id,
                    func=func,
                    args=args,
                    kwargs=kwargs,
                )
            )
            return func(*args, **kwargs)

        @wraps(func)
        async def async_wrapper(
            *args,
            usage_fps: float = 0,
            usage_api_key: Optional[str] = None,
            usage_workflow_id: Optional[str] = None,
            **kwargs,
        ):
            await self.async_record_usage(
                **self._extract_usage_params_from_func_kwargs(
                    usage_fps=usage_fps,
                    usage_api_key=usage_api_key,
                    usage_workflow_id=usage_workflow_id,
                    func=func,
                    args=args,
                    kwargs=kwargs,
                )
            )
            return await func(*args, **kwargs)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    def _cleanup(self):
        self._terminate_collector_thread.set()
        self._collector_thread.join()
        self._terminate_sender_thread.set()
        self._sender_thread.join()


usage_collector = UsageCollector()

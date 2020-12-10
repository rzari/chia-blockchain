import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import traceback
from enum import Enum
import uuid
import time
from typing import Dict, Any, List, Tuple, Optional
from sys import platform
from concurrent.futures import ThreadPoolExecutor
from websockets import serve, ConnectionClosedOK, WebSocketException
from src.cmds.init import chia_init
from src.daemon.windows_signal import kill
from src.util.ws_message import format_response, create_payload
from src.util.json_util import dict_to_json_str
from src.util.config import load_config
from src.util.logging import initialize_logging
from src.util.path import mkdir
from src.util.service_groups import validate_service

io_pool_exc = ThreadPoolExecutor()

try:
    from aiohttp import web
except ModuleNotFoundError:
    print(
        "Error: Make sure to run . ./activate from the project folder before starting Chia."
    )
    quit()

try:
    import fcntl

    has_fcntl = True
except ImportError:
    has_fcntl = False

log = logging.getLogger(__name__)

service_plotter = "chia plots create"


class PlotState(str, Enum):
    SUBMITTED = 'SUBMITTED'
    RUNNING = 'RUNNING'
    ERROR = 'ERROR'
    FINISHED = 'FINISHED'


# determine if application is a script file or frozen exe
if getattr(sys, "frozen", False):
    name_map = {
        "chia": "chia",
        "chia_wallet": "start_wallet",
        "chia_full_node": "start_full_node",
        "chia_harvester": "start_harvester",
        "chia_farmer": "start_farmer",
        "chia_introducer": "start_introducer",
        "chia_timelord": "start_timelord",
        "chia_timelord_launcher": "timelord_launcher",
        "chia_full_node_simulator": "start_simulator",
    }

    def executable_for_service(service_name):
        application_path = os.path.dirname(sys.executable)
        if platform == "win32" or platform == "cygwin":
            executable = name_map[service_name]
            path = f"{application_path}/{executable}.exe"
            return path
        else:
            path = f"{application_path}/{name_map[service_name]}"
            return path


else:
    application_path = os.path.dirname(__file__)

    def executable_for_service(service_name):
        return service_name


class WebSocketServer:
    def __init__(self, root_path):
        self.root_path = root_path
        self.log = log
        self.services: Dict = dict()
        self.plots_queue: List[Dict] = []
        self.connections: Dict[str, List[Any]] = dict()  # service_name : [WebSocket]
        self.remote_address_map: Dict[str, str] = dict()  # remote_address: service_name
        self.ping_job = None
        net_config = load_config(root_path, "config.yaml")
        self.self_hostname = net_config["self_hostname"]
        self.daemon_port = net_config["daemon_port"]

    async def start(self):
        self.log.info("Starting Daemon Server")

        def master_close_cb():
            asyncio.ensure_future(self.stop())

        try:
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGINT, master_close_cb
            )
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGTERM, master_close_cb
            )
        except NotImplementedError:
            self.log.info("Not implemented")

        self.websocket_server = await serve(
            self.safe_handle,
            self.self_hostname,
            self.daemon_port,
            max_size=None,
            ping_interval=500,
            ping_timeout=300,
        )

        self.log.info("Waiting Daemon WebSocketServer closure")
        print("Daemon server started", flush=True)
        await self.websocket_server.wait_closed()
        self.log.info("Daemon WebSocketServer closed")

    def cancel_task_safe(self, task):
        if task is not None:
            try:
                task.cancel()
            except Exception as e:
                self.log.error(f"Error while canceling task.{e} {task}")

    async def stop(self):
        self.cancel_task_safe(self.ping_job)
        await self.exit()
        self.websocket_server.close()
        return {"success": True}

    async def safe_handle(self, websocket, path):
        service_name = ""
        try:
            async for message in websocket:
                try:
                    decoded = json.loads(message)
                    response, sockets_to_use = await self.handle_message(
                        websocket, decoded
                    )
                except Exception as e:
                    tb = traceback.format_exc()
                    self.log.error(f"Error while handling message: {tb}")
                    error = {"success": False, "error": f"{e}"}
                    response = format_response(message, error)
                if len(sockets_to_use) > 0:
                    for socket in sockets_to_use:
                        try:
                            await socket.send(response)
                        except Exception as e:
                            tb = traceback.format_exc()
                            self.log.error(
                                f"Unexpected exception trying to send to websocket: {e} {tb}"
                            )
                            self.remove_connection(socket)
                            await socket.close()
        except Exception as e:
            remote_address = websocket.remote_address[1]
            tb = traceback.format_exc()
            service_name = "Unknown"
            if remote_address in self.remote_address_map:
                service_name = self.remote_address_map[remote_address]
            if isinstance(e, ConnectionClosedOK):
                self.log.info(
                    f"ConnectionClosedOk. Closing websocket with {service_name} {e}"
                )
            elif isinstance(e, WebSocketException):
                self.log.info(
                    f"Websocket exception. Closing websocket with {service_name} {e} {tb}"
                )
            else:
                self.log.error(f"Unexpected exception in websocket: {e} {tb}")
        finally:
            self.remove_connection(websocket)
            await websocket.close()

    def remove_connection(self, websocket):
        remote_address = websocket.remote_address[1]
        service_name = None
        if remote_address in self.remote_address_map:
            service_name = self.remote_address_map[remote_address]
            self.remote_address_map.pop(remote_address)
        if service_name in self.connections:
            after_removal = []
            for connection in self.connections[service_name]:
                if connection.remote_address[1] == remote_address:
                    continue
                else:
                    after_removal.append(connection)
            self.connections[service_name] = after_removal

    async def ping_task(self):
        restart = True
        await asyncio.sleep(30)
        for remote_address, service_name in self.remote_address_map.items():
            if service_name in self.connections:
                sockets = self.connections[service_name]
                for socket in sockets:
                    try:
                        self.log.info(f"About to ping: {service_name}")
                        await socket.ping()
                    except asyncio.CancelledError:
                        self.log.info("Ping task received Cancel")
                        restart = False
                        break
                    except Exception as e:
                        self.log.info(f"Ping error: {e}")
                        self.log.warning("Ping failed, connection closed.")
                        self.remove_connection(socket)
                        await socket.close()
        if restart is True:
            self.ping_job = asyncio.create_task(self.ping_task())

    async def handle_message(
        self, websocket, message
    ) -> Tuple[Optional[str], List[Any]]:
        """
        This function gets called when new message is received via websocket.
        """

        command = message["command"]
        destination = message["destination"]
        if destination != "daemon":
            destination = message["destination"]
            if destination in self.connections:
                sockets = self.connections[destination]
                return dict_to_json_str(message), sockets

            return None, []

        data = None
        if "data" in message:
            data = message["data"]
        if command == "ping":
            response = await self.ping()
        elif command == "start_service":
            response = await self.start_service(data)
        elif command == "start_plotting":
            response = await self.start_plotting(data)
        elif command == "stop_plotting":
            response = await self.stop_plotting(data)
        elif command == "stop_service":
            response = await self.stop_service(data)
        elif command == "is_running":
            response = await self.is_running(data)
        elif command == "exit":
            response = await self.stop()
        elif command == "register_service":
            response = await self.register_service(websocket, data)
        else:
            self.log.error(f"UK>> {message}")
            response = {"success": False, "error": f"unknown_command {command}"}

        full_response = format_response(message, response)
        return (full_response, [websocket])

    async def ping(self):
        response = {"success": True, "value": "pong"}
        return response

    def extract_plot_queue(self):
        data = []
        for item in self.plots_queue:
            data.append(plot_queue_to_payload(item))
        return data

    def plot_queue_to_payload(plot_queue_item):
        error = plot_queue_item.get("error")
        has_error = error is not None

        return {
            "id": plot_queue_item["id"],
            "size": plot_queue_item["size"],
            "parallel": plot_queue_item["parallel"],
            "delay": plot_queue_item["delay"],
            "state": plot_queue_item["state"],
            "error": str(error) if has_error else None,
            "log": plot_queue_item.get("log"),
        }

    async def _state_changed(self, service: str, state: str):
        if service not in self.connections:
            return

        message = None
        websockets = self.connections[service]

        if service == service_plotter:
            message = {
                "state": state,
                "queue": self.extract_plot_queue(),
            }

        if message is None:
            return

        response = create_payload(
            "state_changed", message, service, "wallet_ui"
        )

        for websocket in websockets:
            try:
                await websocket.send(response)
            except Exception as e:
                tb = traceback.format_exc()
                self.log.error(
                    f"Unexpected exception trying to send to websocket: {e} {tb}"
                )
                websockets.remove(websocket)
                await websocket.close()

    def state_changed(self, service: str, state: str):
        asyncio.create_task(self._state_changed(service, state))

    async def _watch_file_changes(self, id: str, loop):
        config = self._get_plots_queue_item(id)

        if config is None:
            raise Exception(f"Plot queue config with ID {id} is not defined")

        words = ['Renamed final file']
        file_path = config["out_file"]
        fp = open(file_path, 'r')
        while True:
            new = await loop.run_in_executor(io_pool_exc, fp.readline)

            config["log"] = new if config["log"] is None else config["log"] + new
            self.state_changed(service_plotter, "log_changed")

            if new:
                for word in words:
                    if word in new:
                        yield (word, new)
            else:
                time.sleep(0.5)

    async def _track_plotting_progress(self, id: str, loop):
        config = self._get_plots_queue_item(id)

        if config is None:
            raise Exception(f"Plot queue config with ID {id} is not defined")

        async for hit_word, hit_sentence in self._watch_file_changes(id, loop):
            break

    def _build_plotting_command_args(self, request):
        service_name = request["service"]

        k = request["k"]
        n = request["n"]
        t = request["t"]
        t2 = request["t2"]
        d = request["d"]
        b = request["b"]
        u = request["u"]
        r = request["r"]
        s = request["s"]
        a = request["a"]

        command_args: List[str] = []
        command_args += service_name.split(" ")
        command_args.append(f"-k={k}")
        command_args.append(f"-n={n}")
        command_args.append(f"-t={t}")
        command_args.append(f"-2={t2}")
        command_args.append(f"-d={d}")
        command_args.append(f"-b={b}")
        command_args.append(f"-u={u}")
        command_args.append(f"-r={r}")
        command_args.append(f"-s={s}")

        if a is not None:
            command_args.append(f"-a={a}")

        return command_args

    def _is_serial_plotting_running(self):
        response = False
        for item in self.plots_queue:
            if item["parallel"] is False and item["state"] is PlotState.RUNNING:
                response = True
        return response

    def _get_plots_queue_item(self, id: str):
        config = next(item for item in self.plots_queue if item["id"] == id)
        return config

    def _run_next_serial_plotting(self, loop):
        next_plot_id = None

        for item in self.plots_queue:
            if item["state"] is PlotState.SUBMITTED and item["parallel"] is False:
                next_plot_id = item["id"]

        if next_plot_id is not None:
            loop.create_task(self._start_plotting(next_plot_id, loop))

    async def _start_plotting(self, id: str, loop):
        current_process = None
        try:
            log.info(f"Starting plotting with ID {id}")
            config = self._get_plots_queue_item(id)

            if config is None:
                raise Exception(f"Plot queue with ID {id} does not exists")

            state = config["state"]
            if state is not PlotState.SUBMITTED:
                raise Exception(f"Plot with ID {id} has no state submitted")

            id = config["id"]
            delay = config["delay"]
            await asyncio.sleep(delay)

            service_name = config["service_name"]
            command_args = config["command_args"]
            process, pid_path = launch_plotter(
                self.root_path, service_name, command_args, id
            )

            current_process = process

            config["state"] = PlotState.RUNNING
            config["out_file"] = plotter_log_path(self.root_path, id).absolute()
            config["process"] = process
            self.state_changed(service_plotter, "state")

            if service_name not in self.services:
                self.services[service_name] = []

            self.services[service_name].append(process)

            await self._track_plotting_progress(id, loop)

            # (output, err) = process.communicate()
            # await process.wait()

            config["state"] = PlotState.FINISHED
            self.state_changed(service_plotter, "state")

        except (subprocess.SubprocessError, IOError):
            log.exception(f"problem starting {service_name}")
            error = Exception("Start plotting failed")
            config["state"] = PlotState.ERROR
            config["error"] = error
            self.state_changed(service_plotter, "state")
            raise error

        finally:
            if current_process is not None:
                self.services[service_name].remove(current_process)
            self._run_next_serial_plotting(loop)

    async def start_plotting(self, request):
        service_name = request["service"]

        delay = request.get("delay", 0)
        parallel = request.get("parallel", False)
        size = request.get("k")

        id = str(uuid.uuid1())
        config = {
            "id": id,
            "size": size,
            "service_name": service_name,
            "command_args": self._build_plotting_command_args(request),
            "parallel": parallel,
            "delay": delay,
            "state": PlotState.SUBMITTED,
            "error": None,
            "log": None,
            "process": None,
        }

        self.plots_queue.append(config)

        if parallel is True or self._is_serial_plotting_running() is False:
            log.info(f"Plotting will start in {delay} seconds")
            loop = asyncio.get_event_loop()
            loop.create_task(self._start_plotting(id, loop))
        else:
            log.info("Plotting will start automatically when previous plotting finish")

        response = {
            "success": True,
            "service_name": service_name,
            "plot_id": str(id),
        }

        return response

    async def stop_plotting(self, request):
        id = request["id"]
        config = self._get_plots_queue_item(id)
        if config is None:
            return {
                "success": False
            }

        id = config["id"]
        state = config["state"]
        process = config["process"]

        try:
            if process is not None and state == PlotState.RUNNING:
                await kill_process(process, self.root_path, service_plotter, id)
            self.plots_queue.remove(config)
            self.state_changed(service_plotter, "removed")
            return {
                "success": True
            }
        except Exception as e:
            log.error(f"Error during killing the plot process: {e}")
            config["state"] = PlotState.ERROR
            config["error"] = str(e)
            self.state_changed(service_plotter, "state")
            pass
            return {
                "success": False
            }

    async def start_service(self, request):
        service_command = request["service"]
        error = None
        success = False
        testing = False
        if "testing" in request:
            testing = request["testing"]

        if not validate_service(service_command):
            error = "unknown service"

        if service_command in self.services:
            service = self.services[service_command]
            r = service is not None and service.poll() is None
            if r is False:
                self.services.pop(service_command)
                error = None
            else:
                error = f"Service {service_command} already running"

        if error is None:
            try:
                exe_command = service_command
                if testing is True:
                    exe_command = f"{service_command} --testing=true"
                process, pid_path = launch_service(self.root_path, exe_command)
                self.services[service_command] = process
                success = True
            except (subprocess.SubprocessError, IOError):
                log.exception(f"problem starting {service_command}")
                error = "start failed"

        response = {"success": success, "service": service_command, "error": error}
        return response

    async def stop_service(self, request):
        service_name = request["service"]
        result = await kill_service(self.root_path, self.services, service_name)
        response = {"success": result, "service_name": service_name}
        return response

    async def is_running(self, request):
        service_name = request["service"]

        if service_name == service_plotter:
            processes = self.services.get(service_name)
            is_running = processes is not None and len(processes) > 0
            response = {
                "success": True,
                "service_name": service_name,
                "is_running": is_running,
            }
        else:
            process = self.services.get(service_name)
            is_running = process is not None and process.poll() is None
            response = {
                "success": True,
                "service_name": service_name,
                "is_running": is_running,
            }

        return response

    async def exit(self):
        jobs = []
        for k in self.services.keys():
            jobs.append(kill_service(self.root_path, self.services, k))
        if jobs:
            await asyncio.wait(jobs)
        self.services.clear()

        # TODO: fix this hack
        asyncio.get_event_loop().call_later(5, lambda *args: sys.exit(0))
        log.info("chia daemon exiting in 5 seconds")

        response = {"success": True}
        return response

    async def register_service(self, websocket, request):
        self.log.info(f"Register service {request}")
        service = request["service"]
        if service not in self.connections:
            self.connections[service] = []
        self.connections[service].append(websocket)

        response = {"success": False}
        if service == service_plotter:
            response = {
                "success": True,
                "service": service,
                "queue": self.extract_plot_queue(),
            }
        else:
            self.remote_address_map[websocket.remote_address[1]] = service
            if self.ping_job is None:
                self.ping_job = asyncio.create_task(self.ping_task())
            response = {"success": True}
        self.log.info(f"registered for service {service}")
        return response


def daemon_launch_lock_path(root_path):
    """
    A path to a file that is lock when a daemon is launching but not yet started.
    This prevents multiple instances from launching.
    """
    return root_path / "run" / "start-daemon.launching"


def pid_path_for_service(root_path, service, id=""):
    """
    Generate a path for a PID file for the given service name.
    """
    pid_name = service.replace(" ", "-").replace("/", "-")
    return root_path / "run" / f"{pid_name}{id}.pid"


def plotter_log_path(root_path, id):
    return root_path / "plotter" / f"plotter_log_{id}.txt"


def launch_plotter(root_path, service_name, service_array, id):
    # we need to pass on the possibly altered CHIA_ROOT
    os.environ["CHIA_ROOT"] = str(root_path)
    service_executable = executable_for_service(service_array[0])

    # Swap service name with name of executable
    service_array[0] = service_executable
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()  # type: ignore
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore

    plotter_path = plotter_log_path(root_path, id)

    if plotter_path.parent.exists():
        if plotter_path.exists():
            plotter_path.unlink()
    else:
        mkdir(plotter_path.parent)
    outfile = open(plotter_path.resolve(), "w")
    log.info(f"Service array: {service_array}")
    process = subprocess.Popen(
        service_array, shell=False, stdout=outfile, startupinfo=startupinfo
    )

    pid_path = pid_path_for_service(root_path, service_name, id)
    try:
        mkdir(pid_path.parent)
        with open(pid_path, "w") as f:
            f.write(f"{process.pid}\n")
    except Exception:
        pass
    return process, pid_path


def launch_service(root_path, service_command):
    """
    Launch a child process.
    """
    # set up CHIA_ROOT
    # invoke correct script
    # save away PID

    # we need to pass on the possibly altered CHIA_ROOT
    os.environ["CHIA_ROOT"] = str(root_path)

    # Innsert proper e
    service_array = service_command.split()
    service_executable = executable_for_service(service_array[0])
    service_array[0] = service_executable
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()  # type: ignore
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore

    # CREATE_NEW_PROCESS_GROUP allows graceful shutdown on windows, by CTRL_BREAK_EVENT signal
    if platform == "win32" or platform == "cygwin":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        creationflags = 0
    process = subprocess.Popen(
        service_array,
        shell=False,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )
    pid_path = pid_path_for_service(root_path, service_command)
    try:
        mkdir(pid_path.parent)
        with open(pid_path, "w") as f:
            f.write(f"{process.pid}\n")
    except Exception:
        pass
    return process, pid_path


async def kill_process(process, root_path, service_name, id, delay_before_kill=15) -> bool:
    pid_path = pid_path_for_service(root_path, service_name, id)

    if platform == "win32" or platform == "cygwin":
        log.info("sending CTRL_BREAK_EVENT signal to %s", service_name)
        # pylint: disable=E1101
        kill(process.pid, signal.SIGBREAK)  # type: ignore

    else:
        log.info("sending term signal to %s", service_name)
        process.terminate()

    count = 0
    while count < delay_before_kill:
        if process.poll() is not None:
            break
        await asyncio.sleep(1)
        count += 1
    else:
        process.kill()
        log.info("sending kill signal to %s", service_name)
    r = process.wait()
    log.info("process %s returned %d", service_name, r)
    try:
        pid_path_killed = pid_path.with_suffix(".pid-killed")
        if pid_path_killed.exists():
            pid_path_killed.unlink()
        os.rename(pid_path, pid_path_killed)
    except Exception:
        pass

    return True


async def kill_service(root_path, services, service_name, delay_before_kill=15) -> bool:
    process = services.get(service_name)
    if process is None:
        return False
    del services[service_name]

    result = await kill_process(process, root_path, service_name, "", delay_before_kill)
    return result


def is_running(services, service_name):
    process = services.get(service_name)
    return process is not None and process.poll() is None


def create_server_for_daemon(root_path):
    routes = web.RouteTableDef()

    services: Dict = dict()

    @routes.get("/daemon/ping/")
    async def ping(request):
        return web.Response(text="pong")

    @routes.get("/daemon/service/start/")
    async def start_service(request):
        service_name = request.query.get("service")
        if not validate_service(service_name):
            r = f"{service_name} unknown service"
            return web.Response(text=str(r))

        if is_running(services, service_name):
            r = f"{service_name} already running"
            return web.Response(text=str(r))

        try:
            process, pid_path = launch_service(root_path, service_name)
            services[service_name] = process
            r = f"{service_name} started"
        except (subprocess.SubprocessError, IOError):
            log.exception(f"problem starting {service_name}")
            r = f"{service_name} start failed"

        return web.Response(text=str(r))

    @routes.get("/daemon/service/stop/")
    async def stop_service(request):
        service_name = request.query.get("service")
        r = await kill_service(root_path, services, service_name)
        return web.Response(text=str(r))

    @routes.get("/daemon/service/is_running/")
    async def is_running_handler(request):
        service_name = request.query.get("service")
        r = is_running(services, service_name)
        return web.Response(text=str(r))

    @routes.get("/daemon/exit/")
    async def exit(request):
        jobs = []
        for k in services.keys():
            jobs.append(kill_service(root_path, services, k))
        if jobs:
            await asyncio.wait(jobs)
        services.clear()

        # we can't await `site.stop()` here because that will cause a deadlock, waiting for this
        # request to exit


def singleton(lockfile, text="semaphore"):
    """
    Open a lockfile exclusively.
    """

    if not lockfile.parent.exists():
        mkdir(lockfile.parent)

    try:
        if has_fcntl:
            f = open(lockfile, "w")
            fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            if lockfile.exists():
                lockfile.unlink()
            fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            f = open(fd, "w")
        f.write(text)
    except IOError:
        return None
    return f


async def async_run_daemon(root_path):
    chia_init(root_path)
    config = load_config(root_path, "config.yaml")
    initialize_logging("daemon", config["logging"], root_path)
    lockfile = singleton(daemon_launch_lock_path(root_path))
    if lockfile is None:
        print("daemon: already launching")
        return 2

    # TODO: clean this up, ensuring lockfile isn't removed until the listen port is open
    create_server_for_daemon(root_path)
    log.info("before start")
    ws_server = WebSocketServer(root_path)
    await ws_server.start()


def run_daemon(root_path):
    return asyncio.get_event_loop().run_until_complete(async_run_daemon(root_path))


def main():
    from src.util.default_root import DEFAULT_ROOT_PATH

    return run_daemon(DEFAULT_ROOT_PATH)


if __name__ == "__main__":
    main()

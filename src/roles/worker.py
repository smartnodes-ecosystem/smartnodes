import hashlib

from src.p2p.torch_node import TorchNode
from src.p2p.connection import Connection
from src.ml.model_analyzer import estimate_memory, handle_output, get_gpu_memory

import torch.nn as nn
import torch.optim as optim
import threading
import pickle
import queue
import torch
import time
import os


class Worker(TorchNode):
    """
    Todo:
        - convert pickling to json for security (?)
        - process other jobs/batches while waiting for worker response (?)
        - link workers to database or download training data for complete offloading
        - different subclasses of Worker for memory requirements to designate memory-specific
            tasks, ie distributing a model too large to handle on a single computer / user
        - function that detaches the huggingface wrapped outputs tensor without modifying the rest
    """

    def __init__(
        self,
        host: str,
        port: int,
        wallet_address: str,
        debug: bool = False,
        max_connections: int = 0,
    ):
        super(Worker, self).__init__(
            host,
            port,
            wallet_address,
            debug=debug,
            max_connections=max_connections,
        )
        self.role = b"W"

        # For storing forward, backward, and intermediate tensors
        # Should be switched to some other data structure that relates to specific epochs
        self.modules = {}
        self.optimizers = {}
        self.parameters = {}
        self.state_updates = {}

        self.loss = None

    def stream_data(self, data: bytes, node: Connection):
        """
                Handle incoming tensors from connected nodes and new job requests

                Todo:
        d            - ensure correct nodes sending data
                    - potentially move/forward method directly to Connection to save data via identifying data
                        type and relevancy as its streamed in, saves bandwidth if we do not need the data / spam
        """

        try:

            handled = super().stream_data(data, node)

            # Try worker-related tags if not found in parent class
            if not handled:

                # The case where we load via downloaded pickle file (potential security threat? & slow)
                if b"DONE STREAM" == data[:11]:
                    file_name = f"streamed_data_{node.host}_{node.port}"

                    with open(file_name, "rb") as f:
                        streamed_bytes = f.read()

                    handled = self.stream_data(streamed_bytes, node)

                    os.remove(file_name)

                    return handled

                # Handle incoming forward pass request
                elif b"FORWARD" == data[:7]:
                    self.debug_print(
                        f"RECEIVED FORWARD: {round((data.__sizeof__() - 5) / 1e6, 1)} MB"
                    )
                    # Master-specific handling (ie for DistributedModel)
                    if self.master:
                        [n_iter, n_micro, module_id], tensor = pickle.loads(data[7:])
                        self.modules["Master"].forward_queues[n_micro].put(
                            ([n_iter, n_micro, module_id], tensor)
                        )

                        return True

                    # Module-specific handling (ie for OffloadedModule / nn.Module)
                    elif self.training and len(self.modules) > 0:
                        print(hashlib.sha256(data).hexdigest())
                        (n_iter, n_micro, module_id), tensor = pickle.loads(data[7:])
                        self.modules[module_id].forward_queues.put(
                            ([n_iter, n_micro], tensor)
                        )

                        return True

                # Handle incoming backward pass request
                elif b"BACKWARD" == data[:8]:
                    self.debug_print(
                        f"RECEIVED BACKWARD: {round((data.__sizeof__() - 5) / 1e6, 1)} MB"
                    )

                    # Master-specific handling (ie for DistributedModel)
                    if self.master:
                        [n_iter, n_micro, module_id], tensor = pickle.loads(data[8:])
                        self.modules["Master"].backward_queues[n_micro].put(
                            ([n_iter, n_micro, module_id], tensor)
                        )

                        return True

                    # Module-specific handling (ie for OffloadedModule / nn.Module)
                    elif self.training and self.modules:
                        (n_iter, n_micro, module_id), tensor = pickle.loads(data[8:])
                        self.modules[module_id].backward_queues.put(
                            ([n_iter, n_micro], tensor)
                        )

                        return True

                # Handle requests for module parameters
                elif b"PARAMSREQ" == data[:9]:
                    self.debug_print(f"RECEIVED PARAMS REQUEST")
                    if self.training:
                        # Must ensure requesting node is indeed the master or an overseeing validator
                        module_id = data[9:]
                        self.send_parameters(
                            node, self.modules[module_id].parameters(), module_id
                        )

                        return True

                # Handle and store responses from a parameters request
                elif b"PARAMETERS" == data[:10]:
                    self.debug_print(f"RECEIVED PARAMS REQUEST")
                    module_id, parameters = pickle.loads(data[10:])
                    self.parameters[module_id] = parameters

                    return True

                # Handle update .train parameter request
                elif b"UT-REQ" == data[:6]:
                    # self.debug_print()
                    if self.training:
                        # Must ensure requesting node is the master
                        mode = False if data[6:7] == b"0" else True
                        module_id = data[7:]
                        self.modules[module_id].training = mode
                        self.send_train_updated(node, mode, module_id)

                        return True

                # Handle update .train parameter change request from master node
                elif b"TU-REQ" == data[:6]:
                    if self.master or self.training:
                        mode = False if data[6:7] == b"0" else True
                        module_id = data[7:]
                        if module_id in self.state_updates.keys():
                            self.state_updates[module_id]["train"] = mode
                        else:
                            self.state_updates[module_id] = {"train": mode}

                        return True

                elif b"JOB" == data[:3]:
                    try:
                        # Accept job request from validator if we can handle it
                        module_id, module_size = pickle.loads(data[3:])

                        if self.available_memory > module_size:
                            # Respond to validator that we can accept the job
                            data = b"ACCEPTJOB" + pickle.dumps(module_id)
                        else:
                            data = b"DECLINEJOB"

                        self.send_to_node(node, data)

                    except Exception as e:
                        raise e

                # elif b"PoL" == data[:3]:
                #     self.debug_print(f"RECEIVED PoL REQUEST")
                #     if self.training and self.model:
                #         dummy_input = pickle.loads(data[3:])
                #
                #         proof_of_learning = self.proof_of_learning(dummy_input)
                #
                #         self.send_to_node(node, proof_of_learning)
                #
                # elif b"TENSOR" == data[:6]:
                #     if self.training:
                #         tensor = pickle.loads(data[6:])
                #
                #         # Confirm identity/role of node
                #         if node in self.inbound:
                #             self.forward_relays.put(tensor)
                #         elif node in self.outbound:
                #             self.backward_relays.put(tensor)

                # Handle receiving module from master node
                elif b"MODULE" == data[:6]:
                    self.debug_print(
                        f"RECEIVED: {round((data.__sizeof__() - 5) / 1e6, 1)} MB"
                    )
                    # Must confirm the model with a job on SC
                    if self.training:
                        # Load in model
                        module = pickle.loads(data[6:])

                        module.forward_queues = queue.Queue()
                        module.backward_queues = queue.Queue()
                        module.intermediates = {}
                        # module.intermediates = queue.LifoQueue()

                        # self.request_statistics()
                        self.modules[module.id] = module
                        self.optimizers[module.id] = optim.Adam(module.parameters())

                        self.debug_print(f"Loaded distributed module!")
                        self.send_to_node(node, b"LOADED" + module.id)

                        return True

                elif b"LOADED" == data[:6]:
                    self.debug_print(f"Successfully offloaded submodule to worker.")
                    pickled = data[6:]
                    self.distributed_graph[pickled] = node

                    return True

                return False

            else:

                return True

        except Exception as e:
            self.debug_print(f"worker:stream_data:{e}")
            raise e

    def run(self):
        # Thread for handling incoming connections
        listener = threading.Thread(target=self.listen, daemon=True)
        listener.start()

        # Thread for periodic worker statistics updates
        stats_updater = threading.Thread(target=self.request_worker_stats, daemon=True)
        stats_updater.start()

        # time.sleep(5)
        # self.updater_flag.set()

        # # Thread for handling incoming tensors from connected nodes (just an idea)
        # data_stream = threading.Thread(target=self.stream_data, daemo=True)
        # data_stream.start()

        # Main worker loop
        while not self.terminate_flag.is_set():
            if (
                self.training and self.port != 5026
            ):  # Port included for master testing without master class
                self.train_loop()

            # Include the following steps:
            # 1. Broadcast GPU memory statistics
            # self.broadcast_statistics()

            # 5. Handle requests for proof of training
            # For example, you can call self.proof_of_optimization(), self.proof_of_output(), etc.

            self.reconnect_nodes()
            time.sleep(1)

        print("Node stopping...")
        for node in self.connections:
            node.stop()

        time.sleep(1)

        for node in self.connections:
            node.join()

        self.sock.settimeout(None)
        self.sock.close()
        print("Node stopped")

    def load_distributed_module(self, module: nn.Module, graph: dict = None):
        pass

    def train_loop(self):
        if self.training:
            # Complete outstanding forward and backward passes
            for module_id, module in self.modules.items():
                # Complete any outstanding back propagations
                if module.backward_queues.empty() is False:
                    next_node = list(self.nodes.values())[
                        0
                    ]  # Placeholder for the connecting node

                    # Grab backwards pass from forward node and our associated input/output from forward pass
                    tag, loss_relay = module.backward_queues.get()
                    inter_tag = tuple(tag)
                    assoc_input, assoc_output = module.intermediates[inter_tag]

                    # Continue backwards pass on our section of model
                    assoc_output.backward(
                        loss_relay, retain_graph=True
                    )  # Do we need retain graph?
                    dvalues = assoc_input.grad

                    tag.append(module_id)

                    # Pass along backwards pass to next node
                    self.send_backward(next_node["connection"], dvalues, tag)
                    self.optimizers[module_id].zero_grad()
                    self.optimizers[module_id].step()

                if module.forward_queues.empty() is False:
                    next_node = list(self.nodes.values())[
                        0
                    ]  # Placeholder for the appropriate node

                    tag, tensor = module.forward_queues.get()

                    # Unpack queued forward pass unpack values (eg. mask, stride...)
                    if isinstance(tensor, tuple):
                        args, kwargs = tensor
                    else:
                        args = tensor
                        kwargs = {}

                    # Clear tensor of any previous info, set up for custom backward pass
                    inp = (
                        handle_output(args).clone().detach().requires_grad_()
                    )  # This should be done on sending node, not receiving
                    out = module(inp, **kwargs)

                    inter_tag = tuple(tag)
                    tag.append(module_id)

                    # Store output and input tensor for backward pass
                    module.intermediates[inter_tag] = [inp, handle_output(out)]

                    # Relay forward pass to the next node
                    self.send_forward(next_node["connection"], out, tag)

    def proof_of_learning(self, dummy_input: torch.Tensor):
        proof = {
            "node_id": self.name,
            "memory": self.available_memory,
            "learning": self.training,
            "model": self.model,
        }

        if self.training:
            proof["output"] = handle_output(self.model(dummy_input)).sum()

    """Key Methods to Implement"""
    # def request_worker(self, nodes, module_memory: int, module_type: int):
    #     # module_type = 0 when model is modular , select worker based on lowest latency and having enough memory
    #     if module_type == 0:
    #         candidate_node = max(nodes, key=lambda x: x["memory"])
    #
    #     # module_type = 1 when model is not modular, select the new master based on both the minimum of a latency
    #     # matrix (low latency to other good workers), and memory
    #     elif module_type == 1:
    #         candidate_node = max(nodes, key=lambda x: x["latency"])

    # def acknowledge(self):
    #     pass

    # def host_job(self, model: nn.Module):
    #     """
    #     Todo:
    #         - connect to master node via SC and load in model
    #         - attempt to assign and relay model to other idle connected workers
    #         - determine relevant connections
    #     """
    #     self.optimizer = torch.optim.Adam
    #     self.training = True
    #     self.distribute_model(model)  # Add master vs worker functionality

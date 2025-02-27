import json
from copy import deepcopy
from typing import Dict, List, Optional, Union

from .config import config as global_config
from .config import s3_config
from .io import PVC, InputParameter, Inputs, OutputParameter, Outputs
from .utils import randstr

try:
    from argo.workflows.client import (V1alpha1Cache, V1alpha1Memoize,
                                       V1alpha1Metadata,
                                       V1alpha1ResourceTemplate,
                                       V1alpha1ScriptTemplate,
                                       V1alpha1Template, V1alpha1UserContainer,
                                       V1ConfigMapKeySelector, V1EnvVar,
                                       V1EnvVarSource, V1ResourceRequirements,
                                       V1SecretKeySelector, V1Volume,
                                       V1VolumeMount)
    from argo.workflows.client.configuration import Configuration

    import kubernetes

    from .client.v1alpha1_retry_strategy import V1alpha1RetryStrategy
except Exception:
    V1alpha1ResourceTemplate = object
    V1alpha1RetryStrategy = object
    V1Volume = object
    V1VolumeMount = object
    V1alpha1UserContainer = object
    V1EnvVarSource = object


class Secret:
    def __init__(self, value):
        self.value = value
        self.secret_name = "dflow-" + randstr(15)
        self.secret_key = "secret"
        data = {self.secret_key: value}
        secret = kubernetes.client.V1Secret(
            string_data=data,
            metadata=kubernetes.client.V1ObjectMeta(name=self.secret_name),
            type="Opaque")
        with get_k8s_client() as k8s_client:
            core_v1_api = kubernetes.client.CoreV1Api(k8s_client)
            core_v1_api.api_client.call_api(
                '/api/v1/namespaces/%s/secrets' % global_config["namespace"],
                'POST', body=secret, response_type='V1Secret',
                header_params=global_config["http_headers"],
                _return_http_data_only=True)


def get_k8s_client(k8s_api_server=None, token=None, k8s_config_file=None):
    if k8s_api_server is None:
        k8s_api_server = global_config["k8s_api_server"]
    if token is None:
        token = global_config["token"]
    if k8s_config_file is None:
        k8s_config_file = global_config["k8s_config_file"]
    if k8s_api_server is not None:
        k8s_configuration = kubernetes.client.Configuration(
            host=k8s_api_server)
        k8s_configuration.verify_ssl = False
        if token is None:
            k8s_client = kubernetes.client.ApiClient(k8s_configuration)
        else:
            k8s_client = kubernetes.client.ApiClient(
                k8s_configuration, header_name='Authorization',
                header_value='Bearer %s' % token)
        return k8s_client
    else:
        return kubernetes.config.new_client_from_config(
            config_file=k8s_config_file)


class OPTemplate:
    def __init__(
            self,
            name: Optional[str] = None,
            inputs: Optional[Inputs] = None,
            outputs: Optional[Outputs] = None,
            memoize_key: Optional[str] = None,
            pvcs: Optional[List[PVC]] = None,
            annotations: Dict[str, str] = None,
    ) -> None:
        if name is None:
            name = randstr()
        # force lowercase to fix RFC 1123
        self.name = name.lower()
        if inputs is not None:
            self.inputs = inputs
        else:
            self.inputs = Inputs(template=self)
        if outputs is not None:
            self.outputs = outputs
        else:
            self.outputs = Outputs(template=self)
        self.memoize_key = memoize_key
        self.memoize = None
        if pvcs is None:
            pvcs = []
        self.pvcs = pvcs
        if annotations is None:
            annotations = {}
        self.annotations = annotations
        self.modified = False

    @classmethod
    def from_dict(cls, d):
        kwargs = {
            "name": d.get("name", None),
            "inputs": Inputs.from_dict(d.get("inputs", {})),
            "outputs": Outputs.from_dict(d.get("outputs", {})),
            "memoize_key": d.get("memoize", {}).get("key", None),
            "annotations": d.get("metadata", {}).get("annotations", None),
        }
        return cls(**kwargs)

    def __setattr__(self, key, value):
        super().__setattr__(key, value)
        if key == "inputs":
            assert isinstance(value, Inputs)
            value.set_template(self)
        elif key == "outputs":
            assert isinstance(value, Outputs)
            value.set_template(self)

    def handle_key(self, memoize_prefix=None, memoize_configmap="dflow"):
        if "dflow_key" in self.inputs.parameters:
            if memoize_prefix is not None:
                self.memoize_key = "%s-{{inputs.parameters.dflow_key}}" \
                    % memoize_prefix

            if global_config["save_keys_in_global_outputs"]:
                if hasattr(self, "image"):
                    self.outputs.parameters["dflow_global"] = OutputParameter(
                        value="{{pod.name}}",
                        global_name="{{inputs.parameters.dflow_key}}",
                    )
                else:
                    self.outputs.parameters["dflow_global"] = OutputParameter(
                        value_from_parameter="non-exists",
                        default="{{node.id}}",
                        global_name="{{inputs.parameters.dflow_key}}",
                    )
        elif "dflow_group_key" in self.inputs.parameters:
            if memoize_prefix is not None:
                self.memoize_key = \
                    "%s-{{inputs.parameters.dflow_group_key}}-init-artifact" \
                    % memoize_prefix

        if self.memoize_key is not None:
            # Is it a bug of Argo?
            if self.memoize_key.find("workflow.name") != -1:
                self.inputs.parameters["workflow_name"] = InputParameter(
                    value="{{workflow.name}}")
                self.memoize_key = self.memoize_key.replace(
                    "workflow.name", "inputs.parameters.workflow_name")
            config = Configuration()
            config.client_side_validation = False
            self.memoize = V1alpha1Memoize(
                key=self.memoize_key,
                local_vars_configuration=config, cache=V1alpha1Cache(
                    config_map=V1ConfigMapKeySelector(
                        name="%s-%s" % (memoize_configmap, self.memoize_key),
                        local_vars_configuration=config)))

    def copy(self):
        if self.modified:
            return self
        new_template = deepcopy(self)
        new_template.name += "-" + randstr()
        new_template.modified = True
        return new_template


class ScriptOPTemplate(OPTemplate):
    """
    Script OP template

    Args:
        name: the name of the OP template
        inputs: input parameters and input artifacts
        outputs: output parameters and output artifacts
        image: image the template uses
        command: command to run the script
        script: script
        volumes: volumes the template uses
        mounts: volumes the template mounts
        init_progress: a str representing the initial progress
        timeout: timeout of the OP template
        retry_strategy: retry strategy of the OP template
        memoize_key: memoized key of the OP template
        pvcs: PVCs need to be declared
        image_pull_policy: Always, IfNotPresent, Never
        annotations: annotations for the OP template
        requests: a dict of resource requests
        limits: a dict of resource limits
        envs: environment variables
    """

    def __init__(
            self,
            name: Optional[str] = None,
            inputs: Optional[Inputs] = None,
            outputs: Optional[Outputs] = None,
            memoize_key: Optional[str] = None,
            pvcs: Optional[List[PVC]] = None,
            annotations: Dict[str, str] = None,
            image: Optional[str] = None,
            command: Union[str, List[str]] = None,
            script: Optional[str] = None,
            volumes: Optional[List[V1Volume]] = None,
            mounts: Optional[List[V1VolumeMount]] = None,
            init_progress: str = "0/1",
            timeout: Optional[str] = None,
            retry_strategy: Optional[V1alpha1RetryStrategy] = None,
            resource: Optional[V1alpha1ResourceTemplate] = None,
            image_pull_policy: Optional[str] = None,
            requests: Dict[str, str] = None,
            limits: Dict[str, str] = None,
            envs: Dict[str, Union[str, Secret, V1EnvVarSource]] = None,
            init_containers: Optional[List[V1alpha1UserContainer]] = None,
    ) -> None:
        super().__init__(name=name, inputs=inputs, outputs=outputs,
                         memoize_key=memoize_key, pvcs=pvcs,
                         annotations=annotations)
        self.image = image
        if isinstance(command, str):
            command = [command]
        self.command = command
        self.script = script
        if volumes is None:
            volumes = []
        self.volumes = volumes
        if mounts is None:
            mounts = []
        self.mounts = mounts
        self.init_progress = init_progress
        self.timeout = timeout
        self.retry_strategy = retry_strategy
        self.resource = resource
        self.image_pull_policy = image_pull_policy
        self.requests = requests
        self.limits = limits
        self.envs = envs
        self.init_containers = init_containers
        self.script_rendered = False

    @classmethod
    def from_dict(cls, d):
        kwargs = {
            "name": d.get("name", None),
            "inputs": Inputs.from_dict(d.get("inputs", {})),
            "outputs": Outputs.from_dict(d.get("outputs", {})),
            "memoize_key": d.get("memoize", {}).get("key", None),
            "annotations": d.get("metadata", {}).get("annotations", None),
            "image": d.get("script", {}).get("image", None),
            "command": d.get("script", {}).get("command", None),
            "script": d.get("script", {}).get("source", None),
            "volumes": d.get("volumes", None),
            "mounts": d.get("script", {}).get("volumeMounts", None),
            "init_progress": d.get("metadata", {}).get("annotations", {}).get(
                "workflows.argoproj.io/progress", "0/1"),
            "timeout": d.get("timeout", None),
            "retry_strategy": d.get("retryStrategy", None),
            "resource": d.get("resource", None),
            "image_pull_policy": d.get("script", {}).get("imagePullPolicy",
                                                         None),
            "requests": d.get("script", {}).get("resources", {}).get(
                "requests", None),
            "limits": d.get("script", {}).get("resources", {}).get(
                "limits", None),
            "envs": {env.name: env.value for env in d.get("script", {}).get(
                "env", [])},
            "init_containers": d.get("initContainers", None),
        }
        engine = d.get("metadata", {}).get("annotations", {}).get(
            "workflow.dp.tech/container_engine")
        docker = "docker" if engine == "docker" else None
        singularity = "singularity" if engine == "singularity" else None
        podman = "podman" if engine == "podman" else None
        if d.get("metadata", {}).get("annotations", {}).get(
                "workflow.dp.tech/executor") == "dispatcher":
            host = kwargs["annotations"].get("workflow.dp.tech/host")
            port = kwargs["annotations"].get("workflow.dp.tech/port", 22)
            username = kwargs["annotations"].get(
                "workflow.dp.tech/username", "root")
            password = kwargs["annotations"].get("workflow.dp.tech/password")
            queue_name = kwargs["annotations"].get(
                "workflow.dp.tech/queue_name")
            extras = kwargs["annotations"].get("workflow.dp.tech/extras")
            extras = json.loads(extras) if extras else {}
            machine = extras.get("machine", None)
            resources = extras.get("resources", None)
            task = extras.get("task", None)
            from .plugins.dispatcher import DispatcherExecutor
            executor = DispatcherExecutor(
                host, queue_name, port, username, password,
                machine_dict=machine, resources_dict=resources,
                task_dict=task, docker_executable=docker,
                singularity_executable=singularity, podman_executable=podman)
            return executor.render(cls(**kwargs))
        elif engine:
            from .executor import ContainerExecutor
            executor = ContainerExecutor(docker, singularity, podman)
            return executor.render(cls(**kwargs))
        return cls(**kwargs)

    def convert_to_argo(self, memoize_prefix=None,
                        memoize_configmap="dflow"):
        self.handle_key(memoize_prefix, memoize_configmap)
        self.annotations["workflows.argoproj.io/progress"] = self.init_progress
        if self.resource is not None:
            return V1alpha1Template(name=self.name,
                                    metadata=V1alpha1Metadata(
                                        annotations=self.annotations),
                                    inputs=self.inputs.convert_to_argo(),
                                    outputs=self.outputs.convert_to_argo(),
                                    timeout=self.timeout,
                                    retry_strategy=self.retry_strategy,
                                    memoize=self.memoize,
                                    volumes=self.volumes,
                                    resource=self.resource,
                                    init_containers=self.init_containers,
                                    )
        else:
            if self.envs is not None:
                env = []
                for k, v in self.envs.items():
                    if isinstance(v, V1EnvVarSource):
                        env.append(V1EnvVar(name=k, value_from=v))
                    elif isinstance(v, Secret):
                        env.append(V1EnvVar(name=k, value_from=V1EnvVarSource(
                            secret_key_ref=V1SecretKeySelector(
                                name=v.secret_name, key=v.secret_key))))
                    else:
                        env.append(V1EnvVar(name=k, value=v))
            else:
                env = None
            if s3_config["prefix"] and s3_config["repo"] is not None:
                loc = deepcopy(s3_config["repo"])
                loc[s3_config["repo_type"]]["key"] = \
                    "%s%s{{workflow.name}}/{{pod.name}}" % (
                        s3_config["repo_prefix"], s3_config["prefix"])
            else:
                loc = None
            return \
                V1alpha1Template(name=self.name,
                                 metadata=V1alpha1Metadata(
                                     annotations=self.annotations),
                                 inputs=self.inputs.convert_to_argo(),
                                 outputs=self.outputs.convert_to_argo(),
                                 timeout=self.timeout,
                                 retry_strategy=self.retry_strategy,
                                 memoize=self.memoize,
                                 volumes=self.volumes,
                                 script=V1alpha1ScriptTemplate(
                                     image=self.image,
                                     image_pull_policy=self.image_pull_policy,
                                     command=self.command, source=self.script,
                                     volume_mounts=self.mounts,
                                     resources=V1ResourceRequirements(
                                         limits=self.limits,
                                         requests=self.requests),
                                     env=env),
                                 init_containers=self.init_containers,
                                 archive_location=loc,
                                 )


class ShellOPTemplate(ScriptOPTemplate):
    """
    Shell script OP template

    Args:
        name: the name of the OP template
        inputs: input parameters and input artifacts
        outputs: output parameters and output artifacts
        image: image the template uses
        command: command to run the script
        script: shell script
        volumes: volumes the template uses
        mounts: volumes the template mounts
        init_progress: a str representing the initial progress
        timeout: timeout of the OP template
        retry_strategy: retry strategy of the OP template
        memoize_key: memoized key of the OP template
        pvcs: PVCs need to be declared
        image_pull_policy: Always, IfNotPresent, Never
        annotations: annotations for the OP template
        requests: a dict of resource requests
        limits: a dict of resource limits
        envs: environment variables
        init_containers: init containers before the template runs
    """

    def __init__(
        self,
        name: Optional[str] = None,
        inputs: Optional[Inputs] = None,
        outputs: Optional[Outputs] = None,
        memoize_key: Optional[str] = None,
        pvcs: Optional[List[PVC]] = None,
        annotations: Dict[str, str] = None,
        image: Optional[str] = None,
        command: Union[str, List[str]] = None,
        script: Optional[str] = None,
        volumes: Optional[List[V1Volume]] = None,
        mounts: Optional[List[V1VolumeMount]] = None,
        init_progress: str = "0/1",
        timeout: Optional[str] = None,
        retry_strategy: Optional[V1alpha1RetryStrategy] = None,
        image_pull_policy: Optional[str] = None,
        requests: Dict[str, str] = None,
        limits: Dict[str, str] = None,
        envs: Dict[str, str] = None,
        init_containers: Optional[List[V1alpha1UserContainer]] = None,
    ) -> None:
        if command is None:
            command = ["sh"]
        super().__init__(
            name=name, inputs=inputs, outputs=outputs, image=image,
            command=command, script=script, volumes=volumes, mounts=mounts,
            init_progress=init_progress, timeout=timeout,
            retry_strategy=retry_strategy, memoize_key=memoize_key, pvcs=pvcs,
            image_pull_policy=image_pull_policy, annotations=annotations,
            requests=requests, limits=limits, envs=envs,
            init_containers=init_containers,
        )


class PythonScriptOPTemplate(ScriptOPTemplate):
    """
    Python script OP template

    Args:
        name: the name of the OP template
        inputs: input parameters and input artifacts
        outputs: output parameters and output artifacts
        image: image the template uses
        command: command to run the script
        script: python script
        volumes: volumes the template uses
        mounts: volumes the template mounts
        init_progress: a str representing the initial progress
        timeout: timeout of the OP template
        retry_strategy: retry strategy of the OP template
        memoize_key: memoized key of the OP template
        pvcs: PVCs need to be declared
        image_pull_policy: Always, IfNotPresent, Never
        annotations: annotations for the OP template
        requests: a dict of resource requests
        limits: a dict of resource limits
        envs: environment variables
        init_containers: init containers before the template runs
    """

    def __init__(
        self,
        name: Optional[str] = None,
        inputs: Optional[Inputs] = None,
        outputs: Optional[Outputs] = None,
        memoize_key: Optional[str] = None,
        pvcs: Optional[List[PVC]] = None,
        annotations: Dict[str, str] = None,
        image: Optional[str] = None,
        command: Union[str, List[str]] = None,
        script: Optional[str] = None,
        volumes: Optional[List[V1Volume]] = None,
        mounts: Optional[List[V1VolumeMount]] = None,
        init_progress: str = "0/1",
        timeout: Optional[str] = None,
        retry_strategy: Optional[V1alpha1RetryStrategy] = None,
        image_pull_policy: Optional[str] = None,
        requests: Dict[str, str] = None,
        limits: Dict[str, str] = None,
        envs: Dict[str, str] = None,
        init_containers: Optional[List[V1alpha1UserContainer]] = None,
    ) -> None:
        if command is None:
            command = ["python3"]
        super().__init__(
            name=name, inputs=inputs, outputs=outputs, image=image,
            command=command, script=script, volumes=volumes, mounts=mounts,
            init_progress=init_progress, timeout=timeout,
            retry_strategy=retry_strategy, memoize_key=memoize_key, pvcs=pvcs,
            image_pull_policy=image_pull_policy, annotations=annotations,
            requests=requests, limits=limits, envs=envs,
            init_containers=init_containers,
        )


class ContainerOPTemplate(ScriptOPTemplate):
    def __init__(
            self,
            command: Union[str, List[str]] = "",
            args: List[str] = None,
            **kwargs):
        if args is None:
            args = []
        if isinstance(command, str):
            command = [command]
        script = "%s %s" % (" ".join(command), " ".join(args))
        kwargs["command"] = ["sh"]
        kwargs["script"] = script
        super().__init__(**kwargs)

    @classmethod
    def from_dict(cls, d):
        kwargs = {
            "name": d.get("name", None),
            "inputs": Inputs.from_dict(d.get("inputs", {})),
            "outputs": Outputs.from_dict(d.get("outputs", {})),
            "memoize_key": d.get("memoize", {}).get("key", None),
            "annotations": d.get("metadata", {}).get("annotations", None),
            "image": d.get("container", {}).get("image", None),
            "command": d.get("container", {})["command"],
            "args": d.get("container", {}).get("args", []),
            "volumes": d.get("volumes", None),
            "mounts": d.get("container", {}).get("volumeMounts", None),
            "init_progress": d.get("metadata", {}).get("annotations", {}).get(
                "workflows.argoproj.io/progress", "0/1"),
            "timeout": d.get("timeout", None),
            "retry_strategy": d.get("retryStrategy", None),
            "resource": d.get("resource", None),
            "image_pull_policy": d.get("container", {}).get("imagePullPolicy",
                                                            None),
            "requests": d.get("container", {}).get("resources", {}).get(
                "requests", None),
            "limits": d.get("container", {}).get("resources", {}).get(
                "limits", None),
            "envs": {env.name: env.value for env in d.get("container", {}).get(
                "env", [])},
            "init_containers": d.get("initContainers", None),
        }
        return cls(**kwargs)

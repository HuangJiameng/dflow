from copy import deepcopy
from typing import Optional, Dict, List, Union

from .config import config, s3_config
from .io import Inputs, Outputs
from .op_template import OPTemplate
from .step import Step

try:
    from argo.workflows.client import V1alpha1Metadata, V1alpha1Template
except Exception:
    pass


class Steps(OPTemplate):
    """
    Steps

    Args:
        name: the name of the steps
        inputs: inputs in the template
        outputs: outputs in the template
        steps: a sequential list of steps
        memoize_key: memoized key of the steps
        annotations: annotations for the OP template
        parallelism: maximum number of running pods for the OP template
        """

    def __init__(
            self,
            name: Optional[str] = None,
            inputs: Optional[Inputs] = None,
            outputs: Optional[Outputs] = None,
            steps: List[Union[Step, List[Step]]] = None,
            memoize_key: Optional[str] = None,
            annotations: Dict[str, str] = None,
            parallelism: Optional[int] = None,
    ) -> None:
        super().__init__(name=name, inputs=inputs, outputs=outputs,
                         memoize_key=memoize_key, annotations=annotations)
        self.parallelism = parallelism
        self.steps = []
        if steps is not None:
            for step in steps:
                self.add(step)

    @classmethod
    def from_dict(cls, d, templates):
        kwargs = {
            "name": d.get("name", None),
            "inputs": Inputs.from_dict(d.get("inputs", {})),
            "outputs": Outputs.from_dict(d.get("outputs", {})),
            "memoize_key": d.get("memoize", {}).get("key", None),
            "annotations": d.get("metadata", {}).get("annotations", None),
            "parallelism": d.get("parallelism", None),
        }
        kwargs["steps"] = [[Step.from_dict(ps, templates) for ps in step]
                           for step in d.get("steps", [[]])]
        return cls(**kwargs)

    def __iter__(self):
        return iter(self.steps)

    def add(
            self,
            step: Union[Step, List[Step]],
    ) -> None:
        """
        Add a step or a list of parallel steps to the steps

        Args:
            step: a step or a list of parallel steps to be added to the
                entrypoint of the workflow
        """
        assert isinstance(step, (Step, list))
        if isinstance(step, Step):
            if step.prepare_step is not None:
                self.steps.append(step.prepare_step)
        elif isinstance(step, list):
            prepare_steps = [ps.prepare_step for ps in step
                             if ps.prepare_step is not None]
            if prepare_steps:
                self.steps.append(prepare_steps)
        self.steps.append(step)
        if isinstance(step, Step):
            if step.check_step is not None:
                self.steps.append(step.check_step)
        elif isinstance(step, list):
            check_steps = [ps.check_step for ps in step
                           if ps.check_step is not None]
            if check_steps:
                self.steps.append(check_steps)

    def convert_to_argo(self, memoize_prefix=None,
                        memoize_configmap="dflow", context=None):
        argo_steps = []
        templates = []
        assert len(self.steps) > 0, "Steps %s is empty" % self.name
        for step in self.steps:
            # each step of steps should be a list of parallel steps, if not,
            # create a sigleton
            if not isinstance(step, list):
                step = [step]
            argo_parallel_steps = []
            for ps in step:
                argo_parallel_steps.append(ps.convert_to_argo(context))
                # template may change after conversion
                templates.append(ps.template)
            argo_steps.append(argo_parallel_steps)

        self.handle_key(memoize_prefix, memoize_configmap)
        argo_template = \
            V1alpha1Template(name=self.name,
                             metadata=V1alpha1Metadata(
                                 annotations=self.annotations),
                             steps=argo_steps,
                             inputs=self.inputs.convert_to_argo(),
                             outputs=self.outputs.convert_to_argo(),
                             memoize=self.memoize,
                             parallelism=self.parallelism)
        return argo_template, templates

    def run(self, workflow_id=None, context=None):
        self.workflow_id = workflow_id
        for step in self:
            if isinstance(step, list):
                from multiprocessing import Process, Queue
                queue = Queue()
                procs = []
                for i, ps in enumerate(step):
                    ps.phase = "Pending"
                    proc = Process(
                        target=ps.run_with_queue,
                        args=(self, context, i, queue, config, s3_config))
                    proc.start()
                    procs.append(proc)

                for i in range(len(step)):
                    # TODO: if the process is killed, this will be blocked
                    # forever
                    j, ps = queue.get()
                    if ps is None:
                        step[j].phase = "Failed"
                        if not step[j].continue_on_failed:
                            raise RuntimeError("Step %s failed" % step[j])
                    else:
                        step[j].outputs = deepcopy(ps.outputs)
            else:
                step.run(self, context)

"""
Validation loop
===============

The lightning validation loop handles everything except the actual computations of your model.
To decide what will happen in your validation loop, define the `validation_step` function.
Below are all the things lightning automates for you in the validation loop.

.. note:: Lightning will run 5 steps of validation in the beginning of training as a sanity
 check so you don't have to wait until a full epoch to catch possible validation issues.

Check validation every n epochs
-------------------------------

If you have a small dataset you might want to check validation every n epochs

.. code-block:: python

    # DEFAULT
    trainer = Trainer(check_val_every_n_epoch=1)

Set how much of the validation set to check
-------------------------------------------

If you don't want to check 100% of the validation set (for debugging or if it's huge), set this flag.

limit_val_batches will be overwritten by overfit_batches if `overfit_batches > 0`

.. code-block:: python

    # DEFAULT
    trainer = Trainer(limit_val_batches=1.0)

    # check 10% only
    trainer = Trainer(limit_val_batches=0.1)

Set how much of the test set to check
-------------------------------------

If you don't want to check 100% of the test set (for debugging or if it's huge), set this flag.

limit_test_batches will be overwritten by overfit_batches if `overfit_batches > 0`

.. code-block:: python

    # DEFAULT
    trainer = Trainer(limit_test_batches=1.0)

    # check 10% only
    trainer = Trainer(limit_test_batches=0.1)

Set validation check frequency within 1 training epoch
------------------------------------------------------

For large datasets it's often desirable to check validation multiple times within a training loop.
 Pass in a float to check that often within 1 training epoch.
 Pass in an int k to check every k training batches. Must use an int if using an IterableDataset.

.. code-block:: python

    # DEFAULT
    trainer = Trainer(val_check_interval=0.95)

    # check every .25 of an epoch
    trainer = Trainer(val_check_interval=0.25)

    # check every 100 train batches (ie: for IterableDatasets or fixed frequency)
    trainer = Trainer(val_check_interval=100)


Set the number of validation sanity steps
-----------------------------------------

Lightning runs a few steps of validation in the beginning of training.
This avoids crashing in the validation loop sometime deep into a lengthy training loop.

.. code-block:: python

    # DEFAULT
    trainer = Trainer(num_sanity_val_steps=2)


You can use `Trainer(num_sanity_val_steps=0)` to skip the sanity check or `Trainer(num_sanity_val_steps=-1)`
to check all the validation data.

# Testing loop

To ensure you don't accidentally use test data to guide training decisions Lightning
 makes running the test set deliberate.

**test**

You have two options to run the test set.
First case is where you test right after a full training routine.

.. code-block:: python

    # run full training
    trainer.fit(model)

    # run test set
    trainer.test()


Second case is where you load a model and run the test set

.. code-block:: python

    model = MyLightningModule.load_from_checkpoint(
        checkpoint_path='/path/to/pytorch_checkpoint.ckpt',
        hparams_file='/path/to/test_tube/experiment/version/hparams.yaml',
        map_location=None
    )

    # init trainer with whatever options
    trainer = Trainer(...)

    # test (pass in the model)
    trainer.test(model)

In this second case, the options you pass to trainer will be used when running
 the test set (ie: 16-bit, dp, ddp, etc...)

"""

from abc import ABC, abstractmethod
from pprint import pprint
from typing import Callable, List, Union

import torch
from torch.utils.data import DataLoader

from pytorch_lightning.core.lightning import LightningModule
from pytorch_lightning.utilities import rank_zero_warn, flatten_dict, AMPType
from pytorch_lightning.core.step_result import EvalResult, Result
from pytorch_lightning.trainer.evaluate_loop import EvaluationLoop

try:
    import torch_xla.distributed.parallel_loader as xla_pl
    import torch_xla.core.xla_model as xm
except ImportError:
    XLA_AVAILABLE = False
else:
    XLA_AVAILABLE = True

try:
    import horovod.torch as hvd
except (ModuleNotFoundError, ImportError):
    HOROVOD_AVAILABLE = False
else:
    HOROVOD_AVAILABLE = True


class TrainerEvaluationLoopMixin(ABC):

    # this is just a summary on variables used in this abstract class,
    #  the proper values/initialisation should be done in child class
    on_gpu: bool
    use_ddp: bool
    use_dp: bool
    use_ddp2: bool
    use_horovod: bool
    use_single_gpu: bool
    data_parallel_device_ids: ...
    model: LightningModule
    num_test_batches: List[int]
    num_val_batches: int
    world_size: int
    fast_dev_run: ...
    process_output: ...
    progress_bar_dict: ...
    global_rank: int
    current_epoch: int
    callback_metrics: ...
    test_dataloaders: DataLoader
    val_dataloaders: DataLoader
    use_tpu: bool
    reload_dataloaders_every_epoch: ...
    tpu_id: int
    verbose_test: bool
    running_sanity_check: bool
    amp_backend: AMPType

    # Callback system
    on_validation_batch_start: Callable
    on_validation_batch_end: Callable
    on_test_batch_start: Callable
    on_test_batch_end: Callable
    on_validation_start: Callable
    on_validation_end: Callable
    on_test_start: Callable
    on_test_end: Callable
    accelerator_backend: ...
    evaluation_loop: EvaluationLoop

    @abstractmethod
    def copy_trainer_model_properties(self, *args):
        """Warning: this is just empty shell for code implemented in other class."""

    @abstractmethod
    def get_model(self) -> LightningModule:
        """Warning: this is just empty shell for code implemented in other class."""

    @abstractmethod
    def is_overridden(self, *args):
        """Warning: this is just empty shell for code implemented in other class."""

    @abstractmethod
    def transfer_batch_to_gpu(self, *args):
        """Warning: this is just empty shell for code implemented in other class."""

    @abstractmethod
    def add_progress_bar_metrics(self, *args):
        """Warning: this is just empty shell for code implemented in other class."""

    @abstractmethod
    def log_metrics(self, *args, **kwargs):
        """Warning: this is just empty shell for code implemented in other class."""

    @abstractmethod
    def reset_test_dataloader(self, *args):
        """Warning: this is just empty shell for code implemented in other class."""

    @abstractmethod
    def reset_val_dataloader(self, *args):
        """Warning: this is just empty shell for code implemented in other class."""

    @abstractmethod
    def call_hook(self, hook_name, *args, **kwargs):
        """Warning: this is just empty shell for code implemented in other class."""

    def _evaluate(
        self,
        model: LightningModule,
        dataloaders: List[DataLoader],
        max_batches: Union[int, List[int]],
        test_mode: bool = False,
    ):
        """Run evaluation code.

        Args:
            model: The model to evaluate.
            dataloaders: A list of PyTorch dataloaders.
            max_batches: An integer or list of integers with length of the number of dataloaders. Each
                entry is the number of batches to process in the corresponding dataloader.
            test_mode:
        """
        # set up the loop for val/test
        self.evaluation_loop.testing = test_mode

        # set up the eval loop
        self.evaluation_loop.setup(model, max_batches, dataloaders)

        # hook
        self.evaluation_loop.on_evaluation_epoch_start()

        # run validation
        for dataloader_idx, dataloader in enumerate(dataloaders):
            dl_outputs = []

            # on TPU we have to wrap it under the ParallelLoader
            dataloader = self.accelerator_backend.process_dataloader(dataloader)

            # each dataloader has a max num batches
            dl_max_batches = self.evaluation_loop.max_batches[dataloader_idx]

            for batch_idx, batch in enumerate(dataloader):
                if batch is None:
                    continue

                # stop short when running on limited batches
                if batch_idx >= dl_max_batches:
                    break

                # val loop hooks
                self.evaluation_loop.on_evaluation_batch_start(batch, batch_idx, dataloader_idx)
                output = self.evaluation_loop.evaluation_step(test_mode, batch, batch_idx, dataloader_idx)
                output = self.evaluation_loop.evaluation_step_end(output)
                self.evaluation_loop.on_evaluation_batch_end(batch, batch_idx, dataloader_idx)

                # clean up
                self.evaluation_loop.evaluation_batch_end_cleanup(output, batch_idx, dataloader_idx)
                self.evaluation_loop.log_metrics(output, batch_idx)

                if output is not None:
                    dl_outputs.append(output)

            self.evaluation_loop.outputs.append(dl_outputs)

        # ---------------------
        # EVAL_EPOCH_END
        # ---------------------
        using_eval_result = self.evaluation_loop.is_using_eval_results()
        eval_results = self.__run_eval_epoch_end(
            test_mode,
            self.evaluation_loop.outputs,
            dataloaders,
            using_eval_result
        )

        # log callback metrics
        self.__update_callback_metrics(eval_results, using_eval_result)

        # Write predictions to disk if they're available.
        self.evaluation_loop.predictions.to_disk()

        # enable train mode again
        model.train()

        # enable gradients to save memory
        torch.set_grad_enabled(True)

        # --------------------------
        # ON_EVAL_EPOCH_END hook
        # --------------------------
        self.evaluation_loop.on_evaluation_epoch_end()

        return eval_results

    def __update_callback_metrics(self, eval_results, using_eval_result):
        if using_eval_result:
            if isinstance(eval_results, list):
                for eval_result in eval_results:
                    self.callback_metrics = eval_result.callback_metrics
            else:
                self.callback_metrics = eval_results.callback_metrics
        else:
            if isinstance(eval_results, list):
                for eval_result in eval_results:
                    # with a scalar return, auto set it to "val_loss" for callbacks
                    if isinstance(eval_result, torch.Tensor):
                        flat = {'val_loss': eval_result}
                    else:
                        flat = flatten_dict(eval_result)
                    self.callback_metrics.update(flat)
            else:
                # with a scalar return, auto set it to "val_loss" for callbacks
                if isinstance(eval_results, torch.Tensor):
                    flat = {'val_loss': eval_results}
                else:
                    flat = flatten_dict(eval_results)
                self.callback_metrics.update(flat)

    def __run_eval_epoch_end(self, test_mode, outputs, dataloaders, using_eval_result):
        model = self.get_model()

        # with a single dataloader don't pass an array
        eval_results = outputs
        if len(dataloaders) == 1:
            eval_results = outputs[0]

        user_reduced = False

        if test_mode:
            if self.is_overridden('test_end', model=model):
                # TODO: remove in v1.0.0
                if using_eval_result:
                    eval_results = self.__gather_epoch_end_eval_results(outputs)

                eval_results = model.test_end(eval_results)
                user_reduced = True
                rank_zero_warn(
                    'Method `test_end` was deprecated in v0.7 and will be removed in v1.0.'
                    ' Use `test_epoch_end` instead.',
                    DeprecationWarning,
                )

            elif self.is_overridden('test_epoch_end', model=model):
                if using_eval_result:
                    eval_results = self.__gather_epoch_end_eval_results(outputs)

                eval_results = model.test_epoch_end(eval_results)
                user_reduced = True

        else:
            if self.is_overridden('validation_end', model=model):
                # TODO: remove in v1.0.0
                if using_eval_result:
                    eval_results = self.__gather_epoch_end_eval_results(outputs)

                eval_results = model.validation_end(eval_results)
                user_reduced = True
                rank_zero_warn(
                    'Method `validation_end` was deprecated in v0.7 and will be removed in v1.0.'
                    ' Use `validation_epoch_end` instead.',
                    DeprecationWarning,
                )

            elif self.is_overridden('validation_epoch_end', model=model):
                if using_eval_result:
                    eval_results = self.__gather_epoch_end_eval_results(outputs)

                eval_results = model.validation_epoch_end(eval_results)
                user_reduced = True

        if using_eval_result and not user_reduced:
            eval_results = self.__auto_reduce_result_objs(outputs)

        if not isinstance(eval_results, list):
            eval_results = [eval_results]

        return eval_results

    def __gather_epoch_end_eval_results(self, outputs):
        eval_results = []
        for epoch_output in outputs:
            result = epoch_output[0].__class__.gather(epoch_output)
            if 'checkpoint_on' in result:
                result.checkpoint_on = result.checkpoint_on.mean()
            if 'early_stop_on' in result:
                result.early_stop_on = result.early_stop_on.mean()

            eval_results.append(result)

        # with 1 dataloader don't pass in a list
        if len(eval_results) == 1:
            eval_results = eval_results[0]
        return eval_results

    def __auto_reduce_result_objs(self, outputs):
        # outputs has a list of results per dataloader
        eval_results = []
        for dl_output in outputs:
            result = dl_output[0]
            result = result.__class__.reduce_on_epoch_end(dl_output)
            if 'checkpoint_on' in result:
                result.checkpoint_on = result.checkpoint_on.mean()
            if 'early_stop_on' in result:
                result.early_stop_on = result.early_stop_on.mean()
            eval_results.append(result)

        return eval_results

    def run_evaluation(self, test_mode: bool = False):
        # hook
        model = self.get_model()
        model.on_pre_performance_check()

        # select dataloaders
        if test_mode:
            self.reset_test_dataloader(model)

            dataloaders = self.test_dataloaders
            max_batches = self.num_test_batches
        else:
            # val
            if self.val_dataloaders is None:
                self.reset_val_dataloader(model)

            dataloaders = self.val_dataloaders
            max_batches = self.num_val_batches

        if dataloaders is None:
            return [], []

        # Validation/Test begin callbacks
        if test_mode:
            self.on_test_start()
        else:
            self.on_validation_start()

        # enable disabling validation step with limit_val_batches = 0
        should_skip = sum(max_batches) == 0
        if should_skip:
            return [], []

        # run evaluation (val_step + val_step_end + val_epoch_end)
        eval_results = self._evaluate(self.model, dataloaders, max_batches, test_mode)

        # log the final eval loop metrics
        eval_loop_results = self.__log_evaluation_epoch_metrics(eval_results, test_mode)

        # hook
        model.on_post_performance_check()

        # eventual dataset reloading
        if test_mode:
            if self.reload_dataloaders_every_epoch:
                self.reset_test_dataloader(model)
        else:
            # val
            if self.reload_dataloaders_every_epoch:
                self.reset_val_dataloader(model)

        # Validation/Test end callbacks
        if test_mode:
            self.on_test_end()
        else:
            self.on_validation_end()

        return eval_loop_results, eval_results

    def __log_evaluation_epoch_metrics(self, eval_results, test_mode):
        eval_loop_results = []
        if eval_results is not None and len(eval_results) > 0:

            # in eval, the user may return something at every validation step without final reduction
            if not isinstance(eval_results, list):
                eval_results = [eval_results]

            for result_idx, result in enumerate(eval_results):
                if isinstance(result, EvalResult):
                    prog_bar_metrics = result.epoch_pbar_metrics
                    log_metrics = result.epoch_log_metrics
                    callback_metrics = result.callback_metrics

                    # in testing we don't need the callback metrics
                    if test_mode:
                        callback_metrics = {}
                else:
                    _, prog_bar_metrics, log_metrics, callback_metrics, _ = self.process_output(result)

                # eval loop returns all metrics
                dataloader_result_metrics = {**prog_bar_metrics, **log_metrics, **callback_metrics}

                # add metrics to prog bar
                self.add_progress_bar_metrics(prog_bar_metrics)

                # log metrics
                self.log_metrics(log_metrics, {})

                # track metrics for callbacks
                self.callback_metrics.update(callback_metrics)

                if len(dataloader_result_metrics) > 0:
                    eval_loop_results.append(dataloader_result_metrics)

        # log results of test
        if test_mode and self.is_global_zero and self.verbose_test:
            print('-' * 80)
            for result_idx, results in enumerate(eval_loop_results):
                print(f'DATALOADER:{result_idx} TEST RESULTS')
                pprint(results)
                print('-' * 80)

        return eval_loop_results

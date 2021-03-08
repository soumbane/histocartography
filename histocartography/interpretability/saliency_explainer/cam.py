import math
from typing import List

import torch
import torch.nn.functional as F

__all__ = ['CAM']
EPS = 10e-7


class _LegacyCAM(object):
    """Implements a class activation map extractor
    Args:
        model (torch.nn.Module): input model
        conv_layer (str): name of the last convolutional layer
    """

    hook_a = None
    hook_handles = []

    def __init__(self, model, conv_layer):

        if not hasattr(model, conv_layer):
            raise ValueError(f"Unable to find submodule {conv_layer} in the model")
        self.model = model
        # Forward hook
        self.hook_handles.append(
            self.model._modules.get(conv_layer).register_forward_hook(self._hook_a)
        )
        # Enable hooks
        self._hooks_enabled = True
        # Should ReLU be used before normalization
        self._relu = False
        # Model output is used by the extractor
        self._score_used = False

    def _hook_a(self, module, input, output):
        """Activation hook"""
        if self._hooks_enabled:
            self.hook_a = output.data

    def clear_hooks(self):
        """Clear model hooks"""
        for handle in self.hook_handles:
            handle.remove()

    @staticmethod
    def _normalize(cams):
        """CAM normalization"""
        cams -= cams.min(-1).values
        cams /= cams.max(-1).values + EPS

        return cams

    def _get_weights(self, class_idx, scores=None):

        raise NotImplementedError

    def _precheck(self, class_idx, scores):
        """Check for invalid computation cases"""

        # Check that forward has already occurred
        if self.hook_a is None:
            raise AssertionError(
                "Inputs need to be forwarded in the model for the conv features to be hooked"
            )
        # Check batch size
        if self.hook_a.shape[0] != 1:
            raise ValueError(
                f"expected a 1-sized batch to be hooked. Received: {self.hook_a.shape[0]}"
            )

        # Check class_idx value
        if class_idx < 0:
            raise ValueError("Incorrect `class_idx` argument value")

        # Check scores arg
        if self._score_used and not isinstance(scores, torch.Tensor):
            raise ValueError(
                "model output scores is required to be passed to compute CAMs"
            )

    def __call__(self, class_idx, scores=None, normalized=True):

        # Integrity check
        # self._precheck(class_idx, scores)

        # Compute CAM
        return self.compute_cams(class_idx, scores, normalized)

    def compute_cams(self, class_idx, scores=None, normalized=True):
        """Compute the CAM for a specific output class
        Args:
            class_idx (int): output class index of the target class whose CAM will be computed
            scores (torch.Tensor[1, K], optional): forward output scores of the hooked model
            normalized (bool, optional): whether the CAM should be normalized
        Returns:
            torch.Tensor[M, N]: class activation map of hooked conv layer
        """

        # Get map weight
        weights = self._get_weights(class_idx, scores)

        # set on cpu if too big for GPU RAM
        is_cuda = weights.is_cuda
        # if weights.shape[0] > 3000:
        #     weights = weights.cpu()
        #     self.hook_a = self.hook_a.cpu()

        # Perform the weighted combination to get the CAM
        num_nodes = self.hook_a.squeeze(0).shape[0]
        batch_cams = (
            weights.unsqueeze(0).repeat(num_nodes, 1) * self.hook_a.squeeze(0)
        ).sum(dim=1)

        if is_cuda:
            batch_cams = batch_cams.cuda()

        if self._relu:
            batch_cams = F.relu(batch_cams, inplace=True)

        # Normalize the CAM
        if normalized:
            batch_cams = self._normalize(batch_cams)

        return batch_cams

    def __repr__(self):
        return f"{self.__class__.__name__}()"


class _CAM(object):
    """Implements a class activation map extractor

    Args:
        model (torch.nn.Module): Input model
        conv_layers (List[str]): List of layers to average
    """

    def __init__(self, model: torch.nn.Module, conv_layers: List[str]) -> None:
        self.hook_a = list()
        self.hook_handles = list()

        self.model = model
        # Forward hooks
        for conv_layer in conv_layers:
            if not hasattr(model, conv_layer):
                raise ValueError(f"Unable to find submodule {conv_layers} in the model")
            self.hook_handles.append(
                self.model._modules.get(conv_layer).register_forward_hook(self._hook_a)
            )
        # Enable hooks
        self._hooks_enabled = True
        # Should ReLU be used before normalization
        self._relu = True
        # Model output is used by the extractor
        self._score_used = False

    def _hook_a(self, module, input, output):
        """Activation hook"""
        if self._hooks_enabled:
            self.hook_a.append(output.data)

    def clear_hooks(self):
        """Clear model hooks"""
        for handle in self.hook_handles:
            handle.remove()

    @staticmethod
    def _normalize(cams):
        """CAM normalization"""
        cams -= cams.min(0).values
        cams /= cams.max(0).values + EPS
        return cams

    def _get_weights(self, class_idx, scores=None):
        raise NotImplementedError

    def _precheck(self, class_idx, scores):
        """Check for invalid computation cases"""

        # Check that forward has already occurred
        if self.hook_a is None:
            raise AssertionError(
                "Inputs need to be forwarded in the model for the conv features to be hooked"
            )

        # Check class_idx value
        if class_idx < 0:
            raise ValueError("Incorrect `class_idx` argument value")

        # Check scores arg
        if self._score_used and not isinstance(scores, torch.Tensor):
            raise ValueError(
                "model output scores is required to be passed to compute CAMs"
            )

    def __call__(self, class_idx, scores=None, normalized=True):

        # Integrity check
        self._precheck(class_idx, scores)

        # Compute CAM
        return self.compute_cams(class_idx, scores, normalized)

    def compute_cams(self, class_idx, scores=None, normalized=True):
        """Compute the CAM for a specific output class
        Args:
            class_idx (int): output class index of the target class whose CAM will be computed
            scores (torch.Tensor[1, K], optional): forward output scores of the hooked model
            normalized (bool, optional): whether the CAM should be normalized
        Returns:
            torch.Tensor[M, N]: class activation map of hooked conv layer
        """

        # Get map weight
        weights = self._get_weights(class_idx, scores)

        # set on cpu if too big for GPU RAM
        is_cuda = weights.is_cuda

        # Perform the weighted combination to get the CAM
        forwards = torch.stack(self.hook_a, dim=2)
        num_nodes = forwards.squeeze(0).shape[0]
        batch_cams = (
            weights.unsqueeze(0).repeat(num_nodes, 1, 1) * forwards.squeeze(0)
        ).sum(dim=1)

        if is_cuda:
            batch_cams = batch_cams.cuda()

        if self._relu:
            batch_cams = F.relu(batch_cams, inplace=True)

        # Normalize the CAM
        if normalized:
            batch_cams = self._normalize(batch_cams)

        # Average out the different weights of the layers
        batch_cams = batch_cams.mean(dim=1)

        return batch_cams

    def __repr__(self):
        return f"{self.__class__.__name__}()"

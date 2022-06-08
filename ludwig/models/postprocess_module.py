from typing import Any, Dict

import torch
from torch import nn

from ludwig.constants import LAST_HIDDEN, LOGITS, NAME, TYPE
from ludwig.features.base_feature import OutputFeature
from ludwig.features.feature_registries import output_type_registry
from ludwig.features.feature_utils import get_module_dict_key_from_name, get_name_from_module_dict_key
from ludwig.utils import output_feature_utils
from ludwig.utils.misc_utils import get_from_registry

EXCLUDE_PRED_SET = {LOGITS, LAST_HIDDEN}


class PostprocessModule(nn.Module):
    """Wraps post processing for prediction outputs.

    The purpose of the module is to be scripted into Torchscript for native serving. The nn.ModuleDict attributes of
    this module use keys generated by feature_utils.get_module_dict_key_from_name in order to prevent name collisions
    with keywords reserved by TorchScript.

    TODO(geoffrey): Implement torchscript-compatible feature_utils.LudwigFeatureDict to replace
    get_module_dict_key_from_name and get_name_from_module_dict_key usage.
    """

    def __init__(self, config: Dict[str, Any], training_set_metadata: Dict[str, Any]):
        super().__init__()

        output_features: Dict[str, OutputFeature] = {
            feature[NAME]: get_from_registry(feature[TYPE], output_type_registry)
            for feature in config["output_features"]
        }
        self.postproc_modules = nn.ModuleDict()
        for feature_name, feature in output_features.items():
            module_dict_key = get_module_dict_key_from_name(feature_name)
            self.postproc_modules[module_dict_key] = feature.create_postproc_module(training_set_metadata[feature_name])

    def forward(self, inputs: Dict[str, torch.Tensor]):
        with torch.no_grad():
            # Turn flat inputs into nested predictions per feature name
            predictions: Dict[str, Dict[str, torch.Tensor]] = {}
            for predict_key, tensor_values in inputs.items():
                feature_name = output_feature_utils.get_feature_name_from_concat_name(predict_key)
                tensor_name = output_feature_utils.get_tensor_name_from_concat_name(predict_key)
                if feature_name not in predictions:
                    predictions[feature_name] = {}
                predictions[feature_name][tensor_name] = tensor_values

            postproc_outputs: Dict[str, Any] = {}
            for module_dict_key, postproc in self.postproc_modules.items():
                feature_name = get_name_from_module_dict_key(module_dict_key)
                # Flatten out the predictions to support Triton input/output
                for tensor_name, postproc_value in postproc(predictions[feature_name]).items():
                    postproc_key = output_feature_utils.get_feature_concat_name(feature_name, tensor_name)
                    postproc_outputs[postproc_key] = postproc_value

            return postproc_outputs

import logging
from typing import Dict, List, Optional, Tuple, Union

import torch
from botorch.models.transforms.input import ReversibleInputTransform

from lume_model.models import BaseModel
from lume_model.variables import InputVariable, OutputVariable

logger = logging.getLogger(__name__)


class PyTorchModel(BaseModel):
    def __init__(
        self,
        model_file: str,
        input_variables: Dict[str, InputVariable],
        output_variables: Dict[str, OutputVariable],
        input_transformers: Optional[List[ReversibleInputTransform]] = [],
        output_transformers: Optional[List[ReversibleInputTransform]] = [],
        output_format: Optional[Dict[str, str]] = {"type": "tensor"},
        feature_order: Optional[list] = None,
        output_order: Optional[list] = None,
    ) -> None:
        """Initializes the model and stores inputs/outputs.

        Args:
            model_file (str): Path to model file generated with torch.save()
            input_variables (Dict[str, InputVariable]): list of model input variables
            output_variables (Dict[str, OutputVariable]): list of model output variables
            input_transformers: (List[ReversibleInputTransform]): list of transformer
                objects to apply to input before passing to model
            output_transformers: (List[ReversibleInputTransform]): list of transformer
                objects to apply to output of model
            output_format (Optional[dict]): Wrapper for interpreting outputs. This now handles
                raw or softmax values, but should be expanded to accomodate misc
                functions. Now, dictionary should look like:
                    {"type": Literal["raw", "string", "tensor", "variable"]}
            feature_order: List[str]: list containing the names of features in the
                order in which they are passed to the model
            output_order: List[str]: list containing the names of outputs in the
                order the model produces them

        TODO: make list of Transformer objects into botorch ChainedInputTransform?

        """
        super(BaseModel, self).__init__()

        # Save init
        self.input_variables = input_variables
        self.output_variables = output_variables
        self._model_file = model_file
        self._output_format = output_format

        # make sure all of the transformers are in eval mode
        self._input_transformers = input_transformers
        for transformer in self._input_transformers:
            transformer.eval()
        self._output_transformers = output_transformers
        for transformer in self._output_transformers:
            transformer.eval()

        self._model = torch.load(model_file).double()
        self._model.eval()

        self._feature_order = feature_order
        self._output_order = output_order

    @property
    def features(self):
        return self._feature_order

    @property
    def outputs(self):
        return self._output_order

    @property
    def input_transformers(self):
        return self._input_transformers

    @property
    def output_transformers(self):
        return self._output_transformers

    @input_transformers.setter
    def input_transformers(self, new_transformer: Tuple[ReversibleInputTransform, int]):
        transformer, loc = new_transformer
        self._input_transformers.insert(loc, transformer)

    @output_transformers.setter
    def output_transformers(
        self, new_transformer: Tuple[ReversibleInputTransform, int]
    ):
        transformer, loc = new_transformer
        self._output_transformers.insert(loc, transformer)

    def evaluate(
        self,
        input_variables: Dict[str, Union[InputVariable, float, torch.Tensor]],
    ) -> Dict[str, Union[torch.Tensor, OutputVariable, float]]:
        """Evaluate model using new input variables.

        Args:
            input_variables (Dict[str, InputVariable]): List of updated input
                variables

        Returns:
            Dict[str, torch.Tensor]: Dictionary mapping var names to outputs

        """
        # all PyTorch models will follow the same process, the inputs
        # are formatted, then converted to model features. Then they
        # are passed through the model, and transformed again on the
        # other side. The final dictionary is then converted into a
        # useful form
        input_vals = self._prepare_inputs(input_variables)
        input_vals = self._arrange_inputs(input_vals)
        features = self._transform_inputs(input_vals)
        raw_output = self._model(features)
        transformed_output = self._transform_outputs(raw_output)
        output = self._parse_outputs(transformed_output)
        output = self._prepare_outputs(output)

        return output

    def _prepare_inputs(
        self, input_variables: Dict[str, Union[InputVariable, float, torch.Tensor]]
    ) -> Dict[str, torch.Tensor]:
        """
        Prepares the input variables dictionary as a format appropriate
        to be passed to the transformers and updates the stored InputVariables
        with new values

        Args:
            input_variables (dict): Dictionary of input variable names to
                variables in any format (InputVariable or raw values)

        Returns:
            dict (Dict[str, torch.Tensor]): dictionary of input variable
                values to be passed to the transformers
        """
        for var_name, var in self.input_variables.items():
            try:
                if isinstance(input_variables[var_name], InputVariable):
                    var.value = input_variables[var_name].value
                elif isinstance(input_variables[var_name], float):
                    var.value = input_variables[var_name]
                else:
                    var.value = input_variables[var_name].item()
            except KeyError as e:
                # NOTE should we be using the default value here, or the previous
                # value?
                logger.info(f"{e} missing from input_dict, using default value")
                var.value = var.default
        # we want to make sure that everything is a tensor at the end of this
        input_variables = {}
        for var_name, var in self.input_variables.items():
            if not torch.is_tensor(var.value):
                # by default we assume that we want the gradients to maintain
                # differentiability
                input_variables[var_name] = torch.tensor(
                    var.value, dtype=torch.double, requires_grad=True
                )
            else:
                input_variables[var_name] = var.value

        return input_variables

    def _arrange_inputs(self, input_variables: Dict[str, torch.Tensor]) -> torch.Tensor:
        """
        Enforces the order of the input variables to be passed to the transformers
        and models

        Args:
            input_variables (dict): Dictionary of input variable names to raw
                values of inputs

        Returns:
            torch.Tensor: ordered tensor of input variables to be passed to the
                transformers

        """
        features = []
        if self._feature_order is not None:
            for feature_name in self._feature_order:
                features.append(input_variables[feature_name])
        else:
            # if there's no order specified, we assume it's the same as the
            # order passed in the variables.yml file
            for feature_name in self.input_variables.keys():
                features.append(input_variables[feature_name])

        return torch.stack(features)

    def _transform_inputs(self, input_values: torch.Tensor) -> torch.Tensor:
        """
        Applies transformations to the inputs

        Args:
            input_values (torch.Tensor): tensor of input variables to be passed
                to the transformers

        Returns:
            torch.Tensor: tensor of transformed input variables to be passed
                to the model
        """
        for transformer in self._input_transformers:
            input_values = transformer(input_values)
        return input_values

    def _transform_outputs(self, model_output: torch.Tensor) -> torch.Tensor:
        """
        Untransforms the model outputs to real units

        Args:
            model_output (torch.Tensor): tensor of outputs from the model

        Returns:
            Dict[str, torch.Tensor]: dictionary of variable name to tensor
                of untransformed output variables
        """
        # NOTE do we need to sort these to reverse them?
        for transformer in self._output_transformers:
            model_output = transformer.untransform(model_output)
        return model_output

    def _parse_outputs(self, model_output: torch.Tensor) -> Dict[str, torch.Tensor]:
        """
        Constructs dictionary from model outputs

        Args:
            model_output (torch.Tensor): transformed output from NN model

        Returns:
            Dict[str, torch.Tensor]: dictionary of output variable name to output
                value
        """
        output = {}
        if self._output_order is not None:
            for idx, output_name in enumerate(self._output_order):
                output[output_name] = model_output[idx]
        else:
            # if there's no order specified, we assume it's the same as the
            # order passed in the variables.yml file
            for idx, output_name in enumerate(self.output_variables.keys()):
                output[output_name] = model_output[idx]
        return output

    def _prepare_outputs(
        self, predicted_output: Dict[str, torch.Tensor]
    ) -> Dict[str, Union[OutputVariable, torch.Tensor]]:
        """
        Updates the output variables within the model to reflect the new values.
        Args:
            predicted_output (Dict[str, torch.Tensor]): Dictionary of output
                variable name to value

        Returns:
            Dict[str, Union[OutputVariable,torch.Tensor]]: Dictionary of output
                variable name to output tensor or OutputVariable depending
                on model's _ouptut_format
        """
        for variable in self.output_variables.values():
            if variable.variable_type == "scalar":
                self.output_variables[variable.name].value = predicted_output[
                    variable.name
                ].item()

            elif variable.variable_type == "image":
                # OutputVariables should be numpy arrays so we need to convert
                # the tensor to a numpy array
                self.output_variables[variable.name].value = (
                    predicted_output[variable.name].reshape(variable.shape).numpy()
                )

                # update limits
                if self.output_variables[variable.name].x_min_variable:
                    self.output_variables[variable.name].x_min = predicted_output[
                        self.output_variables[variable.name].x_min_variable
                    ].item()

                if self.output_variables[variable.name].x_max_variable:
                    self.output_variables[variable.name].x_max = predicted_output[
                        self.output_variables[variable.name].x_max_variable
                    ].item()

                if self.output_variables[variable.name].y_min_variable:
                    self.output_variables[variable.name].y_min = predicted_output[
                        self.output_variables[variable.name].y_min_variable
                    ].item()

                if self.output_variables[variable.name].y_max_variable:
                    self.output_variables[variable.name].y_max = predicted_output[
                        self.output_variables[variable.name].y_max_variable
                    ].item()
        if self._output_format.get("type") == "tensor":
            return predicted_output
        elif self._output_format.get("type") == "variable":
            return self.output_variables
        else:
            return {key: var.value for key, var in self.output_variables.items()}

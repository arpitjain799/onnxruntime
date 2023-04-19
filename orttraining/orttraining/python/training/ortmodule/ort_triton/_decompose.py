# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

# Decompose a complicated op into a series of simple ops.

import numpy as np
from onnx import GraphProto, NodeProto, TensorProto, helper

from ._utils import get_attribute, get_reduce_info, to_numpy_array


# TODO: share constants.
class DecomposeDispatch(object):
    def __init__(self):
        super().__init__()
        self.count = 0

    def _get_unique_var_name(self, prefix):
        self.count += 1
        return prefix + str(self.count)

    def _new_node(self, op_type, inputs, name, output=None, **kwargs):
        name = self._get_unique_var_name(name)
        if output is None:
            output = [self._get_unique_var_name(f"{op_type}_out")]
        return output[0], helper.make_node(op_type, inputs, output, name, **kwargs)

    def __call__(self, node: NodeProto, graph: GraphProto, **kwargs):
        if not hasattr(self, node.op_type):
            raise NotImplementedError("Not implemented for op type: {}".format(node.op_type))
        return getattr(self, node.op_type)(node, graph, **kwargs)

    def __contains__(self, node_or_op_type):
        op_type = node_or_op_type.op_type if isinstance(node_or_op_type, NodeProto) else node_or_op_type
        return isinstance(op_type, str) and hasattr(self, op_type)

    def LayerNormalization(self, node: NodeProto, graph: GraphProto, **kwargs):
        node_arg_infos = kwargs["node_arg_infos"]
        input_type = node_arg_infos[node.input[0]].dtype
        is_half = input_type == TensorProto.FLOAT16 or input_type == TensorProto.BFLOAT16
        output = node.output[0]
        mean_output = node.output[1] if len(node.output) > 1 else None
        inv_std_dev_output = node.output[2] if len(node.output) > 2 else None

        axis = get_attribute(node, "axis", -1)

        epsilon_tensor = helper.make_tensor(
            name="epsilon_const",
            data_type=TensorProto.FLOAT if is_half else input_type,
            dims=(1,),
            vals=np.array([get_attribute(node, "epsilon", 1e-05)]),
        )

        const_out, const_node = self._new_node("Constant", [], f"{node.name}_const", value=epsilon_tensor)
        input = node.input[0]
        cast_node = None
        if is_half:
            input, cast_node = self._new_node("Cast", [input], f"{node.name}_cast", to=TensorProto.FLOAT)
        reducemean_out, reducemean_node = self._new_node(
            "ReduceMean", [input], f"{node.name}_reducemean", output=[mean_output], axes=[axis]
        )
        sub_out, sub_node = self._new_node("Sub", [input, reducemean_out], f"{node.name}_sub")
        mul_out, mul_node = self._new_node("Mul", [sub_out, sub_out], f"{node.name}_mul")
        reducemean_out1, reducemean_node1 = self._new_node(
            "ReduceMean", [mul_out], f"{node.name}_reducemean1", axes=[axis]
        )
        add_out, add_node = self._new_node("Add", [reducemean_out1, const_out], f"{node.name}_add")
        rsqrt_out, rsqrt_node = self._new_node("Rsqrt", [add_out], f"{node.name}_rsqrt", output=[inv_std_dev_output])
        mul_out1, mul_node1 = self._new_node("Mul", [sub_out, rsqrt_out], f"{node.name}_mul1")
        cast_node1 = None
        if is_half:
            mul_out1, cast_node1 = self._new_node("Cast", [mul_out1], f"{node.name}_cast1", to=input_type)
        mul_out2, mul_node2 = self._new_node("Mul", [node.input[1], mul_out1], f"{node.name}_mul2")
        _, add_node1 = self._new_node("Add", [node.input[2], mul_out2], f"{node.name}_add1", output=[output])

        return list(
            filter(
                lambda node: node is not None,
                [
                    const_node,
                    cast_node,
                    reducemean_node,
                    sub_node,
                    mul_node,
                    reducemean_node1,
                    add_node,
                    rsqrt_node,
                    mul_node1,
                    cast_node1,
                    mul_node2,
                    add_node1,
                ],
            )
        )

    # def LayerNormalizationGrad(self, node: NodeProto, **kwargs):
    #     # dy, x, scale, mean, inv_std_dev -> dx, dscale, dbias
    #     node_arg_infos = kwargs["node_arg_infos"]
    #     input_type = node_arg_infos[node.input[0]][0]
    #     is_half = input_type == TensorProto.FLOAT16 or input_type == TensorProto.BFLOAT16
    #     dx_output = node.output[0]
    #     dscale_output = node.output[1] if len(node.output) > 1 else None
    #     dbias_output = node.output[2] if len(node.output) > 2 else None

    #     axis = get_attribute(node, "axis", -1)

    #     dy_input = node.input[0]
    #     x_input = node.input[1]
    #     scale_input = node.input[2]
    #     cast_node = None
    #     cast_node1 = None
    #     cast_node2 = None
    #     if is_half:
    #         dy_input, cast_node = self._new_node("Cast", [dy_input], f"{node.name}_cast", to=TensorProto.FLOAT)
    #         x_input, cast_node1 = self._new_node("Cast", [x_input], f"{node.name}_cast1", to=TensorProto.FLOAT)
    #         scale_input, cast_node2 = self._new_node("Cast", [scale_input], f"{node.name}_cast2", to=TensorProto.FLOAT)
    #     sub_out, sub_node = self._new_node("Sub", [x_input, node.input[3]], f"{node.name}_sub")
    #     mul_out, mul_node = self._new_node("Mul", [sub_out, node.input[4]], f"{node.name}_mul")
    #     mul_out1, mul_node1 = self._new_node("Mul", [scale_input, dy_input], f"{node.name}_mul1")
    #     mul_out2, mul_node2 = self._new_node("Mul", [mul_out, mul_out1], f"{node.name}_mul2")
    #     reducemean_out, reducemean_node = self._new_node("ReduceMean", [mul_out2], f"{node.name}_reducemean", axes=[axis])
    #     reducemean_out1, reducemean_node1 = self._new_node(
    #         "ReduceMean", [mul_out1], f"{node.name}_reducemean1", axes=[axis]
    #     )
    #     mul_out3, mul_node3 = self._new_node("Mul", [reducemean_out, mul_out], f"{node.name}_mul3")
    #     add_out, add_node = self._new_node("Add", [mul_out3, reducemean_out1], f"{node.name}_add")
    #     sub_out1, sub_node1 = self._new_node("Sub", [mul_out1, add_out], f"{node.name}_sub1")
    #     cast_node3 = None
    #     if is_half:
    #         mul_out4, mul_node4 = self._new_node("Mul", [sub_out1, node.input[4]], f"{node.name}_mul4")
    #         _, cast_node3 = self._new_node("Cast", [mul_out4], f"{node.name}_cast3", output=[dx_output], to=input_type)
    #     else:
    #         _, mul_node4 = self._new_node("Mul", [sub_out1, node.input[4]], f"{node.name}_mul4", output=[dx_output])

    #     # TODO: handle dscale and dbias
    #     return []

    def Softmax(self, node: NodeProto, graph: GraphProto, **kwargs):
        node_arg_infos = kwargs["node_arg_infos"]
        input_type = node_arg_infos[node.input[0]].dtype
        is_half = input_type == TensorProto.FLOAT16 or input_type == TensorProto.BFLOAT16

        axis = get_attribute(node, "axis", -1)

        max_out, max_node = self._new_node("ReduceMax", [node.input[0]], f"{node.name}_max", axes=[axis])
        sub_out, sub_node = self._new_node("Sub", [node.input[0], max_out], f"{node.name}_sub")
        cast_node = None
        if is_half:
            sub_out, cast_node = self._new_node("Cast", [sub_out], f"{node.name}_cast", to=TensorProto.FLOAT)
        exp_out, exp_node = self._new_node("Exp", [sub_out], f"{node.name}_exp")
        sum_out, sum_node = self._new_node("ReduceSum", [exp_out], f"{node.name}_sum", axes=[axis])
        cast_node1 = None
        if is_half:
            div_out, div_node = self._new_node("Div", [exp_out, sum_out], f"{node.name}_div")
            _, cast_node1 = self._new_node("Cast", [div_out], f"{node.name}_cast1", output=node.output, to=input_type)
        else:
            _, div_node = self._new_node("Div", [exp_out, sum_out], f"{node.name}_div", output=node.output)

        return list(
            filter(
                lambda node: node is not None,
                [max_node, sub_node, cast_node, exp_node, sum_node, div_node, cast_node1],
            )
        )

    def SoftmaxGrad_13(self, node: NodeProto, graph: GraphProto, **kwargs):
        node_arg_infos = kwargs["node_arg_infos"]
        input_type = node_arg_infos[node.input[0]].dtype
        is_half = input_type == TensorProto.FLOAT16 or input_type == TensorProto.BFLOAT16

        axis = get_attribute(node, "axis", -1)

        dy_input = node.input[0]
        y_input = node.input[1]
        cast_node = None
        cast_node1 = None
        if is_half:
            dy_input, cast_node = self._new_node("Cast", [dy_input], f"{node.name}_cast", to=TensorProto.FLOAT)
            y_input, cast_node1 = self._new_node("Cast", [y_input], f"{node.name}_cast1", to=TensorProto.FLOAT)
        mul_out, mul_node = self._new_node("Mul", [dy_input, y_input], f"{node.name}_mul")
        sum_out, sum_node = self._new_node("ReduceSum", [mul_out], f"{node.name}_sum", axes=[axis])
        mul_out1, mul_node1 = self._new_node("Mul", [y_input, sum_out], f"{node.name}_mul1")
        cast_node2 = None
        if is_half:
            sub_out, sub_node = self._new_node("Sub", [mul_out, mul_out1], f"{node.name}_sub")
            _, cast_node2 = self._new_node("Cast", [sub_out], f"{node.name}_cast2", output=node.output, to=input_type)
        else:
            _, sub_node = self._new_node("Sub", [mul_out, mul_out1], f"{node.name}_sub", output=node.output)

        return list(
            filter(
                lambda node: node is not None,
                [cast_node, cast_node1, mul_node, sum_node, mul_node1, sub_node, cast_node2],
            )
        )

    def _decompose_reduce_axes(self, node: NodeProto, graph: GraphProto, **kwargs):
        input_rank = len(kwargs["node_arg_infos"][node.input[0]].shape)
        keep_dims, axes = get_reduce_info(node, graph, input_rank)
        if len(axes) == 0:
            return [self._new_node("Identity", [node.input[0]], f"{node.name}_identity", output=node.output)[1]]
        if len(axes) == 1:
            return [node]
        result = []
        input = node.input[0]
        for i in range(len(axes) - 1, -1, -1):
            output = node.output if i == 0 else None
            input, reduce_node = self._new_node(
                node.op_type, [input], f"{node.name}_{i}", output=output, axes=[axes[i]], keepdims=keep_dims
            )
            result.append(reduce_node)
        return result

    def ReduceMax(self, node: NodeProto, graph: GraphProto, **kwargs):
        return self._decompose_reduce_axes(node, graph, **kwargs)

    def ReduceMin(self, node: NodeProto, graph: GraphProto, **kwargs):
        return self._decompose_reduce_axes(node, graph, **kwargs)

    def ReduceSum(self, node: NodeProto, graph: GraphProto, **kwargs):
        return self._decompose_reduce_axes(node, graph, **kwargs)

    def ReduceMean(self, node: NodeProto, graph: GraphProto, **kwargs):
        axes_decompose_result = self._decompose_reduce_axes(node, graph, **kwargs)
        if len(axes_decompose_result) != 1 or axes_decompose_result[0] != node:
            return axes_decompose_result
        node_arg_infos = kwargs["node_arg_infos"]
        input_type = node_arg_infos[node.input[0]].dtype
        input_shape = node_arg_infos[node.input[0]].shape
        is_half = input_type == TensorProto.FLOAT16 or input_type == TensorProto.BFLOAT16
        input_rank = len(input_shape)
        keep_dims, axes = get_reduce_info(node, graph, input_rank)
        assert len(axes) == 1
        axis = axes[0]

        input = node.input[0]
        cast_node = None
        if is_half:
            input, cast_node = self._new_node("Cast", [input], f"{node.name}_cast", to=TensorProto.FLOAT)
        sum_out, sum_node = self._new_node("ReduceSum", [input], f"{node.name}_sum", axes=[axis], keepdims=keep_dims)
        # If it's not concrete shape, we need add more Ops such as Shape, Gather to get the dim value,
        # which is not supported yet.
        assert input_shape[axis].is_number
        axis_dim_tensor = helper.make_tensor(
            name="last_dim",
            dims=(),
            data_type=TensorProto.FLOAT,
            vals=np.array([input_shape[axis]], dtype=np.float32),
        )
        axis_dim_out, axis_dim_node = self._new_node("Constant", [], f"{node.name}_const1", value=axis_dim_tensor)
        cast_node1 = None
        if is_half:
            div_out, div_node = self._new_node("Div", [sum_out, axis_dim_out], f"{node.name}_div")
            _, cast_node1 = self._new_node("Cast", [div_out], f"{node.name}_cast1", output=node.output, to=input_type)
        else:
            _, div_node = self._new_node("Div", [sum_out, axis_dim_out], f"{node.name}_div", output=node.output)

        return list(
            filter(
                lambda node: node is not None,
                [cast_node, sum_node, axis_dim_node, div_node, cast_node1],
            )
        )

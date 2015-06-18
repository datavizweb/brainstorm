#!/usr/bin/env python
# coding=utf-8
from __future__ import division, print_function, unicode_literals

from collections import OrderedDict
import itertools

import numpy as np

from brainstorm.structure.shapes import (
    get_feature_size, validate_shape_template, ShapeTemplate)
from brainstorm.utils import (NetworkValidationError, flatten,
                              convert_to_nested_indices, sort_by_index_key,
                              get_normalized_path)


class Hub(object):

    @staticmethod
    def create(source_set, sink_set, layout, connections):
        # get buffer type for hub and assert its uniform
        btypes = [validate_shape_template(get_by_path(s, layout)['@shape'])
                  for s in flatten(source_set)]
        assert min(btypes) == max(btypes)
        btype = btypes[0]
        # max context size
        context_size = max([get_by_path(s, layout).get('@context_size', 0)
                            for s in flatten(source_set)])

        hub = Hub(sorted(source_set), sorted(sink_set), btype, context_size)
        hub.setup(connections)
        hub.sizes = [get_feature_size(get_by_path(s, layout)['@shape'])
                     for s in hub.sources]
        hub.size = sum(hub.sizes)
        return hub

    def __init__(self, sources, sinks, btype, context_size=0):
        self.sources = sources
        self.sinks = sinks
        self.btype = btype
        self.context_size = context_size
        self.connection_table = []
        self.sizes = []
        self.size = -1

    def setup(self, connections):
        self.set_up_connection_table(connections)
        self.permute_rows()

    def set_up_connection_table(self, connections):
        """
        Construct a source/sink connection table from a list of connections.
        :type connections: list[tuple[object, object]]
        :rtype: np.ndarray
        """
        # set up connection table
        flat_sources = list(flatten(self.sources))
        self.connection_table = np.zeros((len(flat_sources), len(self.sinks)))
        for start, stop in connections:
            if start in self.sources and stop in self.sinks:
                start_idx = flat_sources.index(start)
                stop_idx = self.sinks.index(stop)
                self.connection_table[start_idx, stop_idx] = 1

    def permute_rows(self):
        """
        Given a list of sources and a connection table, find a permutation of
        the sources, such that they can be connected to the sinks via a single
        buffer.
        """
        flat_sources = list(flatten(self.sources))
        nested_indices = convert_to_nested_indices(self.sources)
        # systematically try all permutations until one satisfies the condition
        for perm in itertools.permutations(nested_indices):
            perm = list(flatten(perm))
            ct = np.atleast_2d(self.connection_table[perm])
            if Hub.can_be_connected_with_single_buffer(ct):
                self.connection_table = ct
                self.sources = [flat_sources[i] for i in perm]
                return

        raise NetworkValidationError("Failed to lay out buffers. "
                                     "Please change connectivity.")

    @staticmethod
    def can_be_connected_with_single_buffer(connection_table):
        """
        Check for a connection table if it represents a layout that can be
        realized by a single buffer.

        This means checking if in every column of the table all the ones form a
        connected block.

        Parameters
        ----------
        connection_table : array_like
            2d array of zeros and ones representing the connectivity between
            inputs and outputs of a hub.

        Returns
        -------
        bool
        """
        padded = np.zeros((connection_table.shape[0] + 2,
                           connection_table.shape[1]))
        padded[1:-1, :] = connection_table
        return np.all(np.abs(np.diff(padded, axis=0)).sum(axis=0) <= 2)

    def get_indices(self):
        idxs = np.cumsum([0] + self.sizes)
        for source_name, start, stop in zip(self.sources, idxs, idxs[1:]):
            yield source_name, (int(start), int(stop))

        for i, sink_name in enumerate(self.sinks):
            start = idxs[np.argmax(self.connection_table[:, i])]
            stop = idxs[self.connection_table.shape[0] -
                        np.argmax(self.connection_table[::-1, i])]
            yield sink_name, (int(start), int(stop))


def create_layout(layers):
    # gather connections and order-constraints
    forced_orders = get_forced_orders(layers)
    connections = get_connections(layers)

    # create a stub layout
    layout = create_layout_stub(layers)
    all_sinks, all_sources = get_all_sinks_and_sources(forced_orders,
                                                       connections, layout)

    # group into hubs and lay them out
    hubs = group_into_hubs(all_sources, forced_orders, connections, layout)
    hubs = sorted(hubs, key=lambda x: x.btype)
    layout_hubs(hubs, layout)

    # add shape to parameters
    param_slice = layout['parameters']['@slice']
    layout['parameters']['@shape'] = (param_slice[1] - param_slice[0],)

    return hubs, layout


def layout_hubs(hubs, layout):
    """
    Determine and fill in the @slice entries into the layout and return total
    buffer sizes.
    """
    for hub_nr, hub in enumerate(hubs):
        for buffer_name, _slice in hub.get_indices():
            buffer_layout = get_by_path(buffer_name, layout)
            buffer_layout['@slice'] = _slice
            buffer_layout['@hub'] = hub_nr


def get_all_sinks_and_sources(forced_orders, connections, layout):
    """Gather all sinks and sources while preserving order of the sources."""
    all_sinks = sorted(list(zip(*connections))[1])
    all_sources = list()
    for s in gather_array_nodes(layout):
        if s in all_sinks:
            continue
        for fo in forced_orders:
            if s in set(flatten(all_sources)):
                break
            elif s in fo:
                all_sources.append(fo)
                break
        else:
            all_sources.append(s)
    return all_sinks, all_sources


def get_forced_orders(layers):
    forced_orders = [get_parameter_order(n, l) for n, l in layers.items()]
    forced_orders += [get_internal_order(n, l) for n, l in layers.items()]
    forced_orders = list(filter(None, forced_orders))
    # ensure no overlap
    for fo in forced_orders:
        for other in forced_orders:
            if fo is other:
                continue
            intersect = set(fo) & set(other)
            assert not intersect, "Forced orders may not overlap! but {} " \
                                  "appear(s) in multiple.".format(intersect)
    return forced_orders


def create_layout_stub(layers):
    root = {'@type': 'BufferView',
            'parameters': {
                '@type': 'array',
                '@index': 0
            }}
    for i, (layer_name, layer) in enumerate(layers.items(), start=1):
        root[layer_name] = get_layout_stub_for_layer(layer)
        root[layer_name]['@type'] = 'BufferView'
        root[layer_name]['@index'] = i
    return root


def get_layout_stub_for_layer(layer):
    layout = {}

    layout['inputs'] = {
        k: convert_to_array_json(layer.in_shapes[k], i)
        for i, k in enumerate(sorted(layer.in_shapes))
    }
    layout['inputs']['@type'] = 'BufferView'
    layout['inputs']['@index'] = 0

    layout['outputs'] = {
        k: convert_to_array_json(layer.out_shapes[k], i)
        for i, k in enumerate(sorted(layer.out_shapes))
    }
    layout['outputs']['@type'] = 'BufferView'
    layout['outputs']['@index'] = 1

    parameters = layer.get_parameter_structure()
    assert isinstance(parameters, OrderedDict)
    layout['parameters'] = {
        k: convert_to_array_json(v, i)
        for i, (k, v) in enumerate(parameters.items())
    }
    layout['parameters']['@type'] = 'BufferView'
    layout['parameters']['@index'] = 2

    internals = layer.get_internal_structure()
    assert isinstance(parameters, OrderedDict)

    layout['internals'] = {
        k: convert_to_array_json(v, i)
        for i, (k, v) in enumerate(internals.items())
    }
    layout['internals']['@type'] = 'BufferView'
    layout['internals']['@index'] = 3

    return layout


def convert_to_array_json(shape_template, i):
    assert isinstance(shape_template, ShapeTemplate)
    d = shape_template.to_json()
    d['@type'] = 'array'
    d['@index'] = i
    return d


def get_by_path(path, layout):
    current_node = layout
    for p in path.split('.'):
        try:
            current_node = current_node[p]
        except KeyError:
            raise KeyError('Path "{}" could not be resolved. Key "{}" missing.'
                           .format(path, p))
    return current_node


def gather_array_nodes(layout):
    for k, v in sorted(layout.items(), key=sort_by_index_key):
        if k.startswith('@'):
            continue
        if isinstance(v, dict) and v['@type'] == 'BufferView':
            for sub_path in gather_array_nodes(v):
                yield k + '.' + sub_path
        elif isinstance(v, dict) and v['@type'] == 'array':
            yield k


def get_connections(layers):
    connections = []
    for layer_name, layer in layers.items():
        for con in layer.outgoing:
            start = get_normalized_path(con.start_layer, 'outputs',
                                        con.output_name)
            end = get_normalized_path(con.end_layer, 'inputs', con.input_name)
            connections.append((start, end))

    # add connections to implicit 'parameters'-layer
    for layer_name, layer in layers.items():
        for param_name in layer.get_parameter_structure():
            start = get_normalized_path(layer_name, 'parameters', param_name)
            end = 'parameters'
            connections.append((start, end))

    return sorted(connections)


def get_order(structure):
    return tuple(sorted(structure, key=lambda x: structure[x]['@index']))


def get_parameter_order(layer_name, layer):
    return tuple([get_normalized_path(layer_name, 'parameters', o)
                  for o in layer.get_parameter_structure()])


def get_internal_order(layer_name, layer):
    return tuple([get_normalized_path(layer_name, 'internals', o)
                  for o in layer.get_internal_structure()])


def merge_connections(connections, forced_orders):
    """
    Replace connection nodes with forced order lists if they are part of it.
    """
    merged_connections = []
    for start, stop in connections:
        for fo in forced_orders:
            if start in fo:
                start = fo
            if stop in fo:
                stop = fo
        merged_connections.append((start, stop))
    return merged_connections


def group_into_hubs(remaining_sources, forced_orders, connections, layout):
    m_cons = merge_connections(connections, forced_orders)
    hubs = []
    while remaining_sources:
        node = remaining_sources[0]
        source_set, sink_set = get_forward_closure(node, m_cons)
        for s in source_set:
            remaining_sources.remove(s)

        hubs.append(Hub.create(source_set, sink_set, layout, connections))

    return hubs


def get_forward_closure(node, connections):
    """
    For a given node return two sets of nodes such that:
      - the given node is in the source_set
      - the sink_set contains all the connection targets for nodes of the
        source_set
      - the source_set contains all the connection starts for nodes from the
        sink_set

    :param node: The node to start the forward closure from.
    :param connections: list of nodes
    :type connections: list
    :return: A tuple (source_set, sink_set) where source_set is set of
        nodes containing the initial node and all nodes connecting to nodes
        in the sink_set. And sink_set is a set of nodes containing all
        nodes receiving connections from any of the nodes from the source_set.
    :rtype: (set, set)
    """
    source_set = {node}
    sink_set = {end for start, end in connections if start in source_set}
    growing = True
    while growing:
        growing = False
        new_source_set = {start for start, end in connections
                          if end in sink_set}
        new_sink_set = {end for start, end in connections
                        if start in source_set}
        if len(new_source_set) > len(source_set) or\
                len(new_sink_set) > len(sink_set):
            growing = True
            source_set = new_source_set
            sink_set = new_sink_set
    return source_set, sink_set




import json
import logging
from typing import Union, List, Dict, Any


import numpy
import h5py

import chainer
from chainer import cuda
from chainer import functions as F
from chainer import links as L
from chainer import Variable

from .data import UnicodeCharsVocabulary
from .elmo_lstm import ElmoLstm
from .file_utils import cached_path
from .highway import Highway
from .scalar_mix import ScalarMix


ConfigurationError = ValueError

logger = logging.getLogger(__name__)


def add_sentence_boundary_token_ids(tensor,
                                    mask,
                                    sentence_begin_token,
                                    sentence_end_token):
    """
    Add begin/end of sentence tokens to the batch of sentences.
    Given a batch of sentences with size ``(batch_size, timesteps)`` or
    ``(batch_size, timesteps, dim)`` this returns a tensor of shape
    ``(batch_size, timesteps + 2)`` or ``(batch_size, timesteps + 2, dim)`` respectively.

    Returns both the new tensor and updated mask.

    Parameters
    ----------
    tensor : ``torch.Tensor``
        A tensor of shape ``(batch_size, timesteps)`` or ``(batch_size, timesteps, dim)``
    mask : ``torch.Tensor``
         A tensor of shape ``(batch_size, timesteps)``
    sentence_begin_token: Any (anything that can be broadcast in torch for assignment)
        For 2D input, a scalar with the <S> id. For 3D input, a tensor with length dim.
    sentence_end_token: Any (anything that can be broadcast in torch for assignment)
        For 2D input, a scalar with the </S> id. For 3D input, a tensor with length dim.

    Returns
    -------
    tensor_with_boundary_tokens : ``torch.Tensor``
        The tensor with the appended and prepended boundary tokens. If the input was 2D,
        it has shape (batch_size, timesteps + 2) and if the input was 3D, it has shape
        (batch_size, timesteps + 2, dim).
    new_mask : ``torch.Tensor``
        The new mask for the tensor, taking into account the appended tokens
        marking the beginning and end of the sentence.
    """
    xp = cuda.get_array_module(mask)
    # TODO: matthewp, profile this transfer
    sequence_lengths = cuda.to_cpu(mask.sum(axis=1))
    tensor_shape = list(tensor.shape)
    new_shape = list(tensor_shape)
    new_shape[1] = tensor_shape[1] + 2
    tensor_with_boundary_tokens = xp.zeros(new_shape).astype('i')

    # TODO: gpu change
    if len(tensor_shape) == 2:
        tensor_with_boundary_tokens[:, 1:-1] = \
            tensor
        tensor_with_boundary_tokens[:, 0] = \
            xp.asarray(sentence_begin_token)
        for i, j in enumerate(sequence_lengths):
            tensor_with_boundary_tokens[i, j + 1] = \
                xp.asarray(sentence_end_token)
        new_mask = (tensor_with_boundary_tokens != 0).astype('i')
    elif len(tensor_shape) == 3:
        tensor_with_boundary_tokens[:, 1:-1, :] = tensor
        for i, j in enumerate(sequence_lengths):
            tensor_with_boundary_tokens[i, 0, :] = \
                xp.asarray(sentence_begin_token)
            tensor_with_boundary_tokens[i, j + 1, :] = \
                xp.asarray(sentence_end_token)
        new_mask = ((tensor_with_boundary_tokens > 0).sum(axis=-1) > 0)\
            .astype('i')
    else:
        raise ValueError(
            "add_sentence_boundary_token_ids only accepts 2D and 3D input")

    return tensor_with_boundary_tokens, new_mask


def remove_sentence_boundaries(tensor,
                               mask):
    """
    Remove begin/end of sentence embeddings from the batch of sentences.
    Given a batch of sentences with size ``(batch_size, timesteps, dim)``
    this returns a tensor of shape ``(batch_size, timesteps - 2, dim)`` after removing
    the beginning and end sentence markers.  The sentences are assumed to be padded on the right,
    with the beginning of each sentence assumed to occur at index 0 (i.e., ``mask[:, 0]`` is assumed
    to be 1).
    Returns both the new tensor and updated mask.
    This function is the inverse of ``add_sentence_boundary_token_ids``.
    Parameters
    ----------
    tensor : ``torch.Tensor``
        A tensor of shape ``(batch_size, timesteps, dim)``
    mask : ``torch.Tensor``
         A tensor of shape ``(batch_size, timesteps)``
    Returns
    -------
    tensor_without_boundary_tokens : ``torch.Tensor``
        The tensor after removing the boundary tokens of shape ``(batch_size, timesteps - 2, dim)``
    new_mask : ``torch.Tensor``
        The new mask for the tensor of shape ``(batch_size, timesteps - 2)``.
    """
    xp = cuda.get_array_module(mask)

    # TODO: matthewp, profile this transfer
    sequence_lengths = mask.sum(axis=1)
    tensor_shape = list(tensor.array.shape)
    new_shape = list(tensor_shape)
    new_shape[1] = tensor_shape[1] - 2
    tensor_without_boundary_tokens = xp.zeros(new_shape, 'f')
    new_mask = xp.zeros((new_shape[0], new_shape[1]), 'i')
    for i, j in enumerate(sequence_lengths):
        if j > 2:
            tensor_without_boundary_tokens[i, :(j - 2), :] = \
                tensor.array[i, 1:(j - 1), :]
            new_mask[i, :(j - 2)] = 1

    return tensor_without_boundary_tokens, new_mask


class Elmo(chainer.Chain):
    """
    Compute ELMo representations using a pre-trained bidirectional language model.

    See "Deep contextualized word representations", Peters et al. for details.

    This module takes character id input and computes ``num_output_representations`` different layers
    of ELMo representations.  Typically ``num_output_representations`` is 1 or 2.  For example, in
    the case of the SRL model in the above paper, ``num_output_representations=1`` where ELMo was included at
    the input token representation layer.  In the case of the SQuAD model, ``num_output_representations=2``
    as ELMo was also included at the GRU output layer.

    In the implementation below, we learn separate scalar weights for each output layer,
    but only run the biLM once on each input sequence for efficiency.

    Parameters
    ----------
    options_file : ``str``, required.
        ELMo JSON options file
    weight_file : ``str``, required.
        ELMo hdf5 weight file
    num_output_representations: ``int``, required.
        The number of ELMo representation layers to output.
    requires_grad: ``bool``, optional
        If True, compute gradient of ELMo parameters for fine tuning.
    do_layer_norm : ``bool``, optional, (default=False).
        Should we apply layer normalization (passed to ``ScalarMix``)?
    dropout : ``float``, optional, (default = 0.5).
        The dropout to be applied to the ELMo representations.
    module : ``torch.nn.Module``, optional, (default = None).
        If provided, then use this module instead of the pre-trained ELMo biLM.
        If using this option, then pass ``None`` for both ``options_file``
        and ``weight_file``.  The module must provide a public attribute
        ``num_layers`` with the number of internal layers and its ``forward``
        method must return a ``dict`` with ``activations`` and ``mask`` keys
        (see `_ElmoBilm`` for an example).  Note that ``requires_grad`` is also
        ignored with this option.
    """

    def __init__(self,
                 options_file,
                 weight_file,
                 num_output_representations=1,
                 requires_grad=False,
                 do_layer_norm=False,
                 dropout=0.5):
        super(Elmo, self).__init__()

        logging.info("Initializing ELMo")

        with self.init_scope():
            self._elmo_lstm = _ElmoBiLm(
                options_file, weight_file, requires_grad=requires_grad)
            self._dropout_ratio = dropout
            self._scalar_mixes = []
            for k in range(num_output_representations):
                scalar_mix = ScalarMix(
                    self._elmo_lstm.num_layers, do_layer_norm=do_layer_norm)
                setattr(self, 'scalar_mix_{}'.format(k), scalar_mix)
                self._scalar_mixes.append(scalar_mix)

    def get_output_dim(self):
        return self._elmo_lstm.get_output_dim()

    def forward(self, inputs):
        """
        Parameters
        ----------
        inputs : ``torch.autograd.Variable``
            Shape ``(batch_size, timesteps, 50)`` of character ids representing the current batch.
            We also accept tensors with additional optional dimensions:
            ``(batch_size, dim0, dim1, ..., dimn, timesteps, 50)``

        Returns
        -------
        Dict with keys:
        ``'elmo_representations'``: ``List[torch.autograd.Variable]``
            A ``num_output_representations`` list of ELMo representations for the input sequence.
            Each representation is shape ``(batch_size, timesteps, embedding_dim)``
        ``'mask'``:  ``torch.autograd.Variable``
            Shape ``(batch_size, timesteps)`` long tensor with sequence mask.
        """
        # reshape the input if needed
        original_shape = inputs.shape
        timesteps, num_characters = original_shape[-2:]
        if len(original_shape) > 3:
            reshaped_inputs = inputs.reshape((-1, timesteps, num_characters))
        else:
            reshaped_inputs = inputs

        # run the biLM
        # no backprop through bilstm for lightening computations
        with chainer.using_config("train", False), \
                chainer.no_backprop_mode():
            bilm_output = self._elmo_lstm.forward(reshaped_inputs)
        layer_activations = bilm_output['activations']
        mask_with_bos_eos = bilm_output['mask']

        # compute the elmo representations
        representations = []
        for i in range(len(self._scalar_mixes)):
            scalar_mix = getattr(self, 'scalar_mix_{}'.format(i))
            representation_with_bos_eos = scalar_mix.forward(
                layer_activations, mask_with_bos_eos)
            representation_without_bos_eos, mask_without_bos_eos = remove_sentence_boundaries(
                representation_with_bos_eos, mask_with_bos_eos
            )
            representations.append(F.dropout(
                representation_without_bos_eos,
                ratio=self._dropout_ratio))

        # reshape if necessary
        if len(original_shape) > 3:
            mask = mask_without_bos_eos.reshape(original_shape[:-1])
            elmo_representations = [representation.reshape(original_shape[:-1] + (-1, ))
                                    for representation in representations]
        else:
            mask = mask_without_bos_eos
            elmo_representations = representations

        layer_activations_without_bos_eos = [
            remove_sentence_boundaries(
                a_layer_activation, mask_with_bos_eos)[0]
            for a_layer_activation in layer_activations]
        return {'elmo_representations': elmo_representations, 'mask': mask,
                'elmo_layers': layer_activations_without_bos_eos}

    @classmethod
    def from_params(cls, params):
        # def from_params(cls, params: Params) -> 'Elmo':
        # Add files to archive
        params.add_file_to_archive('options_file')
        params.add_file_to_archive('weight_file')

        options_file = params.pop('options_file')
        weight_file = params.pop('weight_file')
        requires_grad = params.pop('requires_grad', False)
        num_output_representations = params.pop('num_output_representations')
        do_layer_norm = params.pop_bool('do_layer_norm', False)
        params.assert_empty(cls.__name__)

        return cls(options_file, weight_file, num_output_representations,
                   requires_grad=requires_grad, do_layer_norm=do_layer_norm)


class _ElmoCharacterEncoder(chainer.Chain):
    """
    Compute context sensitive token representation using pretrained biLM.

    This embedder has input character ids of size (batch_size, sequence_length, 50)
    and returns (batch_size, sequence_length + 2, embedding_dim), where embedding_dim
    is specified in the options file (typically 512).

    We add special entries at the beginning and end of each sequence corresponding
    to <S> and </S>, the beginning and end of sentence tokens.

    Note: this is a lower level class useful for advanced usage.  Most users should
    use ``ElmoTokenEmbedder`` or ``allennlp.modules.Elmo`` instead.

    Parameters
    ----------
    options_file : ``str``
        ELMo JSON options file
    weight_file : ``str``
        ELMo hdf5 weight file
    requires_grad: ``bool``, optional
        If True, compute gradient of ELMo parameters for fine tuning.

    The relevant section of the options file is something like:
    .. example-code::

        .. code-block:: python

            {'char_cnn': {
                'activation': 'relu',
                'embedding': {'dim': 4},
                'filters': [[1, 4], [2, 8], [3, 16], [4, 32], [5, 64]],
                'max_characters_per_token': 50,
                'n_characters': 262,
                'n_highway': 2
                }
            }
    """

    def __init__(self,
                 options_file,
                 weight_file,
                 requires_grad=False):
        super(_ElmoCharacterEncoder, self).__init__()

        with open(cached_path(options_file), 'r') as fin:
            self._options = json.load(fin)
        self._weight_file = weight_file

        self.output_dim = self._options['lstm']['projection_dim']
        self.requires_grad = requires_grad

        self._load_weights()

        # Cache the arrays for use in forward -- +1 due to masking.
        self._beginning_of_sentence_characters = self.xp.asarray(
            numpy.array(UnicodeCharsVocabulary.bos_chars) + 1
        )
        self._end_of_sentence_characters = self.xp.asarray(
            numpy.array(UnicodeCharsVocabulary.eos_chars) + 1
        )

    def get_output_dim(self):
        return self.output_dim

    def forward(self, inputs):
        """
        Compute context insensitive token embeddings for ELMo representations.

        Parameters
        ----------
        inputs: ``torch.autograd.Variable``
            Shape ``(batch_size, sequence_length, 50)`` of character ids representing the
            current batch.

        Returns
        -------
        Dict with keys:
        ``'token_embedding'``: ``torch.autograd.Variable``
            Shape ``(batch_size, sequence_length + 2, embedding_dim)`` tensor with context
            insensitive token representations.
        ``'mask'``:  ``torch.autograd.Variable``
            Shape ``(batch_size, sequence_length + 2)`` long tensor with sequence mask.
        """
        # Add BOS/EOS
        mask = ((inputs > 0).sum(axis=-1) > 0)

        character_ids_with_bos_eos, mask_with_bos_eos = add_sentence_boundary_token_ids(
            inputs,
            mask,
            self._beginning_of_sentence_characters,
            self._end_of_sentence_characters
        )

        # the character id embedding
        max_chars_per_token = self._options['char_cnn']['max_characters_per_token']
        # (batch_size * sequence_length, max_chars_per_token, embed_dim)

        character_embedding = F.embed_id(
            character_ids_with_bos_eos.reshape((-1, max_chars_per_token)),
            self._char_embedding_weights
        )

        # run convolutions
        cnn_options = self._options['char_cnn']
        if cnn_options['activation'] == 'tanh':
            activation = F.tanh
        elif cnn_options['activation'] == 'relu':
            activation = F.relu
        else:
            raise ConfigurationError("Unknown activation")

        # (batch_size * sequence_length, embed_dim, max_chars_per_token)
        character_embedding = F.transpose(character_embedding, (0, 2, 1))
        character_embedding = character_embedding[:, :, :, None]
        convs = []
        for i in range(len(self._convolutions)):
            conv = getattr(self, 'char_conv_{}'.format(i))
            convolved = conv(character_embedding)
            # (batch_size * sequence_length, n_filters for this width)
            convolved = F.max(convolved, axis=(2, 3))
            convolved = activation(convolved)
            convs.append(convolved)

        # (batch_size * sequence_length, n_filters)
        token_embedding = F.concat(convs, axis=-1)

        # apply the highway layers (batch_size * sequence_length, n_filters)
        token_embedding = self._highways.forward(token_embedding)

        # final projection  (batch_size * sequence_length, embedding_dim)
        token_embedding = self._projection(token_embedding)

        # reshape to (batch_size, sequence_length, embedding_dim)
        batch_size, sequence_length, _ = character_ids_with_bos_eos.shape

        return {
            'mask': mask_with_bos_eos,
            'token_embedding': token_embedding.reshape((batch_size, sequence_length, -1))
        }

    def _load_weights(self):
        self._load_char_embedding()
        self._load_cnn_weights()
        self._load_highway()
        self._load_projection()

    def _load_char_embedding(self):
        with h5py.File(cached_path(self._weight_file), 'r') as fin:
            char_embed_weights = fin['char_embed'][...]

        weights = numpy.zeros(
            (char_embed_weights.shape[0] + 1, char_embed_weights.shape[1]),
            dtype='float32'
        )
        weights[1:, :] = char_embed_weights

        with self.init_scope():
            self._char_embedding_weights = chainer.Parameter(weights)
            self._char_embedding_weights._requires_grad = self.requires_grad

    def _load_cnn_weights(self):
        cnn_options = self._options['char_cnn']
        filters = cnn_options['filters']
        char_embed_dim = cnn_options['embedding']['dim']

        convolutions = []

        for i, (width, num) in enumerate(filters):
            conv = L.Convolution2D(
                in_channels=char_embed_dim,
                out_channels=num,
                ksize=(width, 1),
                nobias=False
            )
            # load the weights
            with h5py.File(cached_path(self._weight_file), 'r') as fin:
                weight = fin['CNN']['W_cnn_{}'.format(i)][...]
                bias = fin['CNN']['b_cnn_{}'.format(i)][...]

            w_reshaped = numpy.transpose(
                weight.squeeze(axis=0), axes=(2, 1, 0))
            # if w_reshaped.shape != tuple(conv.W.data.shape):
            #     raise ValueError("Invalid weight file")
            w_reshaped = w_reshaped[:, :, :, None]
            conv.W.data[:] = w_reshaped
            conv.b.data[:] = bias

            conv.W._requires_grad = self.requires_grad
            conv.b._requires_grad = self.requires_grad

            convolutions.append(conv)
            with self.init_scope():
                setattr(self, 'char_conv_{}'.format(i), conv)

        self._convolutions = convolutions

    def _load_highway(self):
        # pylint: disable=protected-access
        # the highway layers have same dimensionality as the number of cnn filters
        cnn_options = self._options['char_cnn']
        filters = cnn_options['filters']
        n_filters = sum(f[1] for f in filters)
        n_highway = cnn_options['n_highway']

        # create the layers, and load the weights
        with self.init_scope():
            self._highways = Highway(n_filters, n_highway, activation=F.relu)
        for k in range(n_highway):
            # The AllenNLP highway is one matrix multplication with concatenation of
            # transform and carry weights.
            with h5py.File(cached_path(self._weight_file), 'r') as fin:
                # The weights are transposed due to multiplication order assumptions in tf
                # vs pytorch (tf.matmul(X, W) vs pytorch.matmul(W, X))
                w_transform = numpy.transpose(
                    fin['CNN_high_{}'.format(k)]['W_transform'][...])
                # -1.0 since AllenNLP is g * x + (1 - g) * f(x) but tf is (1 - g) * x + g * f(x)
                w_carry = -1.0 * \
                    numpy.transpose(
                        fin['CNN_high_{}'.format(k)]['W_carry'][...])
                weight = numpy.concatenate([w_transform, w_carry], axis=0)
                self._highways._layers[k].W.data[:] = weight
                self._highways._layers[k].W._requires_grad = self.requires_grad

                b_transform = fin['CNN_high_{}'.format(k)]['b_transform'][...]
                b_carry = -1.0 * fin['CNN_high_{}'.format(k)]['b_carry'][...]
                bias = numpy.concatenate([b_transform, b_carry], axis=0)
                self._highways._layers[k].b.data[:] = bias
                self._highways._layers[k].b._requires_grad = self.requires_grad

    def _load_projection(self):
        cnn_options = self._options['char_cnn']
        filters = cnn_options['filters']
        n_filters = sum(f[1] for f in filters)

        with self.init_scope():
            self._projection = L.Linear(
                n_filters, self.output_dim, nobias=False)
        with h5py.File(cached_path(self._weight_file), 'r') as fin:
            weight = fin['CNN_proj']['W_proj'][...]
            bias = fin['CNN_proj']['b_proj'][...]
            self._projection.W.data[:] = numpy.transpose(weight)
            self._projection.b.data[:] = bias

            self._projection.W._requires_grad = self.requires_grad
            self._projection.b._requires_grad = self.requires_grad


class _ElmoBiLm(chainer.Chain):
    """
    Run a pre-trained bidirectional language model, outputing the activations at each
    layer for weighting together into an ELMo representation (with
    ``allennlp.modules.seq2seq_encoders.Elmo``).  This is a lower level class, useful
    for advanced uses, but most users should use ``allennlp.modules.seq2seq_encoders.Elmo``
    directly.

    Parameters
    ----------
    options_file : ``str``
        ELMo JSON options file
    weight_file : ``str``
        ELMo hdf5 weight file
    requires_grad: ``bool``, optional
        If True, compute gradient of ELMo parameters for fine tuning.
    """

    def __init__(self,
                 options_file,
                 weight_file,
                 requires_grad=False):
        super(_ElmoBiLm, self).__init__()

        with open(cached_path(options_file), 'r') as fin:
            options = json.load(fin)
        if not options['lstm'].get('use_skip_connections'):
            raise ConfigurationError(
                'We only support pretrained biLMs with residual connections')
        with self.init_scope():
            self._token_embedder = _ElmoCharacterEncoder(
                options_file, weight_file, requires_grad=requires_grad)
            self._elmo_lstm = ElmoLstm(input_size=options['lstm']['projection_dim'],
                                       hidden_size=options['lstm']['projection_dim'],
                                       cell_size=options['lstm']['dim'],
                                       num_layers=options['lstm']['n_layers'],
                                       memory_cell_clip_value=options['lstm']['cell_clip'],
                                       state_projection_clip_value=options['lstm']['proj_clip'],
                                       requires_grad=requires_grad)
        self._elmo_lstm.load_weights(weight_file)
        # Number of representation layers including context independent layer
        self.num_layers = options['lstm']['n_layers'] + 1

    def get_output_dim(self):
        return 2 * self._token_embedder.get_output_dim()

    def forward(self, inputs):
        """
        Parameters
        ----------
        inputs: ``torch.autograd.Variable``
            Shape ``(batch_size, timesteps, 50)`` of character ids representing the current batch.

        Returns
        -------
        Dict with keys:

        ``'activations'``: ``List[torch.autograd.Variable]``
            A list of activations at each layer of the network, each of shape
            ``(batch_size, timesteps + 2, embedding_dim)``
        ``'mask'``:  ``torch.autograd.Variable``
            Shape ``(batch_size, timesteps + 2)`` long tensor with sequence mask.

        Note that the output tensors all include additional special begin and end of sequence
        markers.
        """
        token_embedding = self._token_embedder.forward(inputs)
        type_representation = token_embedding['token_embedding']
        mask = token_embedding['mask']

        lstm_outputs = self._elmo_lstm.forward(type_representation, mask)

        # Prepare the output.  The first layer is duplicated.
        output_tensors = [
            F.concat([type_representation, type_representation], axis=-1)
        ]
        for layer_activations in F.split_axis(lstm_outputs, lstm_outputs.shape[0], axis=0):
            output_tensors.append(F.squeeze(layer_activations, 0))

        return {
            'activations': output_tensors,
            'mask': mask,
        }

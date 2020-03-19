# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors,  The HuggingFace Inc. Team and deepset Team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Acknowledgements: Many of the modeling parts here come from the great transformers repository: https://github.com/huggingface/transformers.
Thanks for the great work! """

from __future__ import absolute_import, division, print_function, unicode_literals

import json
import logging
import os
import io
from pathlib import Path

from dotmap import DotMap
from tqdm import tqdm
import copy
import numpy as np
import torch
from torch import nn

logger = logging.getLogger(__name__)

from transformers.modeling_bert import BertModel, BertConfig
from transformers.modeling_roberta import RobertaModel, RobertaConfig
from transformers.modeling_xlnet import XLNetModel, XLNetConfig
from transformers.modeling_albert import AlbertModel, AlbertConfig
from transformers.modeling_xlm_roberta import XLMRobertaModel, XLMRobertaConfig
from transformers.modeling_distilbert import DistilBertModel, DistilBertConfig
from transformers.modeling_utils import SequenceSummary
from transformers.tokenization_bert import load_vocab

# These are the names of the attributes in various model configs which refer to the number of dimensions
# in the output vectors
OUTPUT_DIM_NAMES = ["dim", "hidden_size", "d_model"]
PRETRAINED_CONFIG_ARCHIVE_MAP = {"glove-german-uncased":"https://s3.eu-central-1.amazonaws.com/deepset.ai-farm-models/0.4.1/glove-german-uncased/language_model_config.json"}

class LanguageModel(nn.Module):
    """
    The parent class for any kind of model that can embed language into a semantic vector space. Practically
    speaking, these models read in tokenized sentences and return vectors that capture the meaning of sentences
    or of tokens.
    """

    subclasses = {}

    def __init_subclass__(cls, **kwargs):
        """ This automatically keeps track of all available subclasses.
        Enables generic load() or all specific LanguageModel implementation.
        """
        super().__init_subclass__(**kwargs)
        cls.subclasses[cls.__name__] = cls

    def forward(self, input_ids, padding_mask, **kwargs):
        raise NotImplementedError

    @classmethod
    def from_scratch(cls, model_type, vocab_size):
        if model_type.lower() == "bert":
            model = Bert
        return model.from_scratch(vocab_size)

    @classmethod
    def load(cls, pretrained_model_name_or_path, n_added_tokens=0, language_model_class=None, **kwargs):
        """
        Load a pretrained language model either by

        1. specifying its name and downloading it
        2. or pointing to the directory it is saved in.

        Available remote models:

        * bert-base-uncased
        * bert-large-uncased
        * bert-base-cased
        * bert-large-cased
        * bert-base-multilingual-uncased
        * bert-base-multilingual-cased
        * bert-base-chinese
        * bert-base-german-cased
        * roberta-base
        * roberta-large
        * xlnet-base-cased
        * xlnet-large-cased
        * xlm-roberta-base
        * xlm-roberta-large
        * albert-base-v2
        * albert-large-v2
        * distilbert-base-german-cased
        * distilbert-base-multilingual-cased

        See all supported model variations here: https://huggingface.co/models

        The appropriate language model class is inferred automatically from `pretrained_model_name_or_path`
        or can be manually supplied via `language_model_class`.

        :param pretrained_model_name_or_path: The path of the saved pretrained model or its name.
        :type pretrained_model_name_or_path: str
        :param language_model_class: (Optional) Name of the language model class to load (e.g. `Bert`)
        :type language_model_class: str

        """
        config_file = Path(pretrained_model_name_or_path) / "language_model_config.json"
        if os.path.exists(config_file):
            # it's a local directory in FARM format
            config = json.load(open(config_file))
            language_model = cls.subclasses[config["name"]].load(pretrained_model_name_or_path)
        else:
            if language_model_class is None:
                # it's transformers format (either from model hub or local)
                pretrained_model_name_or_path = str(pretrained_model_name_or_path)
                if "xlm" in pretrained_model_name_or_path and "roberta" in pretrained_model_name_or_path:
                    language_model_class = 'XLMRoberta'
                elif 'roberta' in pretrained_model_name_or_path:
                    language_model_class = 'Roberta'
                elif 'albert' in pretrained_model_name_or_path:
                    language_model_class = 'Albert'
                elif 'distilbert' in pretrained_model_name_or_path:
                    language_model_class = 'DistilBert'
                elif 'bert' in pretrained_model_name_or_path:
                    language_model_class = 'Bert'
                elif 'xlnet' in pretrained_model_name_or_path:
                    language_model_class = 'XLNet'
                elif "word2vec" in pretrained_model_name_or_path.lower() or "glove" in pretrained_model_name_or_path.lower():
                    language_model_class = 'WordEmbedding_LM'

            language_model = cls.subclasses[language_model_class].load(pretrained_model_name_or_path, **kwargs)
            if language_model_class == 'XLMRoberta':
                # TODO: for some reason, the pretrained XLMRoberta has different vocab size in the tokenizer compared to the model this is a hack to resolve that
                n_added_tokens = 3

        if not language_model:
            raise Exception(
                f"Model not found for {pretrained_model_name_or_path}. Either supply the local path for a saved model "
                f"or one of bert/roberta/xlnet/albert/distilbert models that can be downloaded from remote. Here's the list of available "
                f"models: https://farm.deepset.ai/api/modeling.html#farm.modeling.language_model.LanguageModel.load"
            )

        # resize embeddings in case of custom vocab
        if n_added_tokens != 0:
            # TODO verify for other models than BERT
            model_emb_size = language_model.model.resize_token_embeddings(new_num_tokens=None).num_embeddings
            vocab_size = model_emb_size + n_added_tokens
            logger.info(
                f"Resizing embedding layer of LM from {model_emb_size} to {vocab_size} to cope with custom vocab.")
            language_model.model.resize_token_embeddings(vocab_size)
            # verify
            model_emb_size = language_model.model.resize_token_embeddings(new_num_tokens=None).num_embeddings
            assert vocab_size == model_emb_size

        return language_model

    def get_output_dims(self):
        config = self.model.config
        for odn in OUTPUT_DIM_NAMES:
            if odn in dir(config):
                return getattr(config, odn)
        else:
            raise Exception("Could not infer the output dimensions of the language model")

    def freeze(self, layers):
        """ To be implemented"""
        raise NotImplementedError()

    def unfreeze(self):
        """ To be implemented"""
        raise NotImplementedError()

    def save_config(self, save_dir):
        save_filename = Path(save_dir) / "language_model_config.json"
        with open(save_filename, "w") as file:
            setattr(self.model.config, "name", self.__class__.__name__)
            setattr(self.model.config, "language", self.language)
            string = self.model.config.to_json_string()
            file.write(string)

    def save(self, save_dir):
        """
        Save the model state_dict and its config file so that it can be loaded again.

        :param save_dir: The directory in which the model should be saved.
        :type save_dir: str
        """
        # Save Weights
        save_name = Path(save_dir) / "language_model.bin"
        model_to_save = (
            self.model.module if hasattr(self.model, "module") else self.model
        )  # Only save the model it-self
        torch.save(model_to_save.state_dict(), save_name)
        self.save_config(save_dir)

    @classmethod
    def _get_or_infer_language_from_name(cls, language, name):
        if language is not None:
            return language
        else:
            return cls._infer_language_from_name(name)

    @classmethod
    def _infer_language_from_name(cls, name):
        known_languages = (
            "german",
            "english",
            "chinese",
            "indian",
            "french",
            "polish",
            "spanish",
            "multilingual",
        )
        matches = [lang for lang in known_languages if lang in name]
        if len(matches) == 0:
            language = "english"
            logger.warning(
                "Could not automatically detect from language model name what language it is. \n"
                "\t We guess it's an *ENGLISH* model ... \n"
                "\t If not: Init the language model by supplying the 'language' param."
            )
        elif len(matches) > 1:
            raise ValueError(
                "Could not automatically detect from language model name what language it is.\n"
                f"\t Found multiple matches: {matches}\n"
                "\t Please init the language model by manually supplying the 'language' as a parameter.\n"
            )
        else:
            language = matches[0]
            logger.info(
                f"Automatically detected language from language model name: {language}"
            )

        return language

    def formatted_preds(self, logits, samples, ignore_first_token=True,
                        padding_mask=None, **kwargs):
        """
        Extracting vectors from language model (e.g. for extracting sentence embeddings).
        Different pooling strategies and layers are available and will be determined from the object attributes
        `extraction_layer` and `extraction_strategy`. Both should be set via the Inferencer:
        Example:  Inferencer(extraction_strategy='cls_token', extraction_layer=-1)

        :param logits: Tuple of (sequence_output, pooled_output) from the language model.
                       Sequence_output: one vector per token, pooled_output: one vector for whole sequence
        :param samples: For each item in logits we need additional meta information to format the prediction (e.g. input text).
                        This is created by the Processor and passed in here from the Inferencer.
        :param ignore_first_token: Whether to include the first token for pooling operations (e.g. reduce_mean).
                                   Many models have here a special token like [CLS] that you don't want to include into your average of token embeddings.
        :param padding_mask: Mask for the padding tokens. Those will also not be included in the pooling operations to prevent a bias by the number of padding tokens.
        :param kwargs: kwargs
        :return: list of dicts containing preds, e.g. [{"context": "some text", "vec": [-0.01, 0.5 ...]}]
        """

        if not hasattr(self, "extraction_layer") or not hasattr(self, "extraction_strategy"):
            raise ValueError("`extraction_layer` or `extraction_strategy` not specified for LM. "
                             "Make sure to set both, e.g. via Inferencer(extraction_strategy='cls_token', extraction_layer=-1)`")

        # unpack the tuple from LM forward pass
        sequence_output = logits[0][0]
        pooled_output = logits[0][1]

        # aggregate vectors
        if self.extraction_strategy == "pooled":
            if self.extraction_layer != -1:
                raise ValueError(f"Pooled output only works for the last layer, but got extraction_layer = {self.extraction_layer}. Please set `extraction_layer=-1`.)")
            vecs = pooled_output.cpu().numpy()
        elif self.extraction_strategy == "per_token":
            vecs = sequence_output.cpu().numpy()
        elif self.extraction_strategy == "reduce_mean":
            vecs = self._pool_tokens(sequence_output, padding_mask, self.extraction_strategy, ignore_first_token=ignore_first_token)
        elif self.extraction_strategy == "reduce_max":
            vecs = self._pool_tokens(sequence_output, padding_mask, self.extraction_strategy, ignore_first_token=ignore_first_token)
        elif self.extraction_strategy == "cls_token":
            vecs = sequence_output[:, 0, :].cpu().numpy()
        else:
            raise NotImplementedError

        preds = []
        for vec, sample in zip(vecs, samples):
            pred = {}
            pred["context"] = sample.tokenized["tokens"]
            pred["vec"] = vec
            preds.append(pred)
        return preds

    def _pool_tokens(self, sequence_output, padding_mask, strategy, ignore_first_token):

        token_vecs = sequence_output.cpu().numpy()
        # we only take the aggregated value of non-padding tokens
        padding_mask = padding_mask.cpu().numpy()
        ignore_mask_2d = padding_mask == 0
        # sometimes we want to exclude the CLS token as well from our aggregation operation
        if ignore_first_token:
            ignore_mask_2d[:, 0] = True
        ignore_mask_3d = np.zeros(token_vecs.shape, dtype=bool)
        ignore_mask_3d[:, :, :] = ignore_mask_2d[:, :, np.newaxis]
        if strategy == "reduce_max":
            pooled_vecs = np.ma.array(data=token_vecs, mask=ignore_mask_3d).max(axis=1).data
        if strategy == "reduce_mean":
            pooled_vecs = np.ma.array(data=token_vecs, mask=ignore_mask_3d).mean(axis=1).data
        return pooled_vecs


class Bert(LanguageModel):
    """
    A BERT model that wraps HuggingFace's implementation
    (https://github.com/huggingface/transformers) to fit the LanguageModel class.
    Paper: https://arxiv.org/abs/1810.04805

    """

    def __init__(self):
        super(Bert, self).__init__()
        self.model = None
        self.name = "bert"

    @classmethod
    def from_scratch(cls, vocab_size, name="bert", language="en"):
        bert = cls()
        bert.name = name
        bert.language = language
        config = BertConfig(vocab_size=vocab_size)
        bert.model = BertModel(config)
        return bert

    @classmethod
    def load(cls, pretrained_model_name_or_path, language=None, **kwargs):
        """
        Load a pretrained model by supplying

        * the name of a remote model on s3 ("bert-base-cased" ...)
        * OR a local path of a model trained via transformers ("some_dir/huggingface_model")
        * OR a local path of a model trained via FARM ("some_dir/farm_model")

        :param pretrained_model_name_or_path: The path of the saved pretrained model or its name.
        :type pretrained_model_name_or_path: str

        """

        bert = cls()
        if "farm_lm_name" in kwargs:
            bert.name = kwargs["farm_lm_name"]
        else:
            bert.name = pretrained_model_name_or_path
        # We need to differentiate between loading model using FARM format and Pytorch-Transformers format
        farm_lm_config = Path(pretrained_model_name_or_path) / "language_model_config.json"
        if os.path.exists(farm_lm_config):
            # FARM style
            bert_config = BertConfig.from_pretrained(farm_lm_config)
            farm_lm_model = Path(pretrained_model_name_or_path) / "language_model.bin"
            bert.model = BertModel.from_pretrained(farm_lm_model, config=bert_config, **kwargs)
            bert.language = bert.model.config.language
        else:
            # Pytorch-transformer Style
            bert.model = BertModel.from_pretrained(str(pretrained_model_name_or_path), **kwargs)
            bert.language = cls._get_or_infer_language_from_name(language, pretrained_model_name_or_path)
        return bert

    def forward(
        self,
        input_ids,
        segment_ids,
        padding_mask,
        **kwargs,
    ):
        """
        Perform the forward pass of the BERT model.

        :param input_ids: The ids of each token in the input sequence. Is a tensor of shape [batch_size, max_seq_len]
        :type input_ids: torch.Tensor
        :param segment_ids: The id of the segment. For example, in next sentence prediction, the tokens in the
           first sentence are marked with 0 and those in the second are marked with 1.
           It is a tensor of shape [batch_size, max_seq_len]
        :type segment_ids: torch.Tensor
        :param padding_mask: A mask that assigns a 1 to valid input tokens and 0 to padding tokens
           of shape [batch_size, max_seq_len]
        :return: Embeddings for each token in the input sequence.

        """
        output_tuple = self.model(
            input_ids,
            token_type_ids=segment_ids,
            attention_mask=padding_mask,
        )
        if self.model.encoder.output_hidden_states == True:
            sequence_output, pooled_output, all_hidden_states = output_tuple[0], output_tuple[1], output_tuple[2]
            return sequence_output, pooled_output, all_hidden_states
        else:
            sequence_output, pooled_output = output_tuple[0], output_tuple[1]
            return sequence_output, pooled_output

    def enable_hidden_states_output(self):
        self.model.encoder.output_hidden_states = True

    def disable_hidden_states_output(self):
        self.model.encoder.output_hidden_states = False


class Albert(LanguageModel):
    """
    An ALBERT model that wraps the HuggingFace's implementation
    (https://github.com/huggingface/transformers) to fit the LanguageModel class.

    """

    def __init__(self):
        super(Albert, self).__init__()
        self.model = None
        self.name = "albert"

    @classmethod
    def load(cls, pretrained_model_name_or_path, language=None, **kwargs):
        """
        Load a language model either by supplying

        * the name of a remote model on s3 ("albert-base" ...)
        * or a local path of a model trained via transformers ("some_dir/huggingface_model")
        * or a local path of a model trained via FARM ("some_dir/farm_model")

        :param pretrained_model_name_or_path: name or path of a model
        :param language: (Optional) Name of language the model was trained for (e.g. "german").
                         If not supplied, FARM will try to infer it from the model name.
        :return: Language Model

        """
        albert = cls()
        if "farm_lm_name" in kwargs:
            albert.name = kwargs["farm_lm_name"]
        else:
            albert.name = pretrained_model_name_or_path
        # We need to differentiate between loading model using FARM format and Pytorch-Transformers format
        farm_lm_config = Path(pretrained_model_name_or_path) / "language_model_config.json"
        if os.path.exists(farm_lm_config):
            # FARM style
            config = AlbertConfig.from_pretrained(farm_lm_config)
            farm_lm_model = Path(pretrained_model_name_or_path) / "language_model.bin"
            albert.model = AlbertModel.from_pretrained(farm_lm_model, config=config, **kwargs)
            albert.language = albert.model.config.language
        else:
            # Huggingface transformer Style
            albert.model = AlbertModel.from_pretrained(str(pretrained_model_name_or_path), **kwargs)
            albert.language = cls._get_or_infer_language_from_name(language, pretrained_model_name_or_path)
        return albert

    def forward(
        self,
        input_ids,
        segment_ids,
        padding_mask,
        **kwargs,
    ):
        """
        Perform the forward pass of the Albert model.

        :param input_ids: The ids of each token in the input sequence. Is a tensor of shape [batch_size, max_seq_len]
        :type input_ids: torch.Tensor
        :param segment_ids: The id of the segment. For example, in next sentence prediction, the tokens in the
           first sentence are marked with 0 and those in the second are marked with 1.
           It is a tensor of shape [batch_size, max_seq_len]
        :type segment_ids: torch.Tensor
        :param padding_mask: A mask that assigns a 1 to valid input tokens and 0 to padding tokens
           of shape [batch_size, max_seq_len]
        :return: Embeddings for each token in the input sequence.

        """
        output_tuple = self.model(
            input_ids,
            token_type_ids=segment_ids,
            attention_mask=padding_mask,
        )
        if self.model.encoder.output_hidden_states == True:
            sequence_output, pooled_output, all_hidden_states = output_tuple[0], output_tuple[1], output_tuple[2]
            return sequence_output, pooled_output, all_hidden_states
        else:
            sequence_output, pooled_output = output_tuple[0], output_tuple[1]
            return sequence_output, pooled_output

    def enable_hidden_states_output(self):
        self.model.encoder.output_hidden_states = True

    def disable_hidden_states_output(self):
        self.model.encoder.output_hidden_states = False


class Roberta(LanguageModel):
    """
    A roberta model that wraps the HuggingFace's implementation
    (https://github.com/huggingface/transformers) to fit the LanguageModel class.
    Paper: https://arxiv.org/abs/1907.11692

    """

    def __init__(self):
        super(Roberta, self).__init__()
        self.model = None
        self.name = "roberta"

    @classmethod
    def load(cls, pretrained_model_name_or_path, language=None, **kwargs):
        """
        Load a language model either by supplying

        * the name of a remote model on s3 ("roberta-base" ...)
        * or a local path of a model trained via transformers ("some_dir/huggingface_model")
        * or a local path of a model trained via FARM ("some_dir/farm_model")

        :param pretrained_model_name_or_path: name or path of a model
        :param language: (Optional) Name of language the model was trained for (e.g. "german").
                         If not supplied, FARM will try to infer it from the model name.
        :return: Language Model

        """
        roberta = cls()
        if "farm_lm_name" in kwargs:
            roberta.name = kwargs["farm_lm_name"]
        else:
            roberta.name = pretrained_model_name_or_path
        # We need to differentiate between loading model using FARM format and Pytorch-Transformers format
        farm_lm_config = Path(pretrained_model_name_or_path) / "language_model_config.json"
        if os.path.exists(farm_lm_config):
            # FARM style
            config = RobertaConfig.from_pretrained(farm_lm_config)
            farm_lm_model = Path(pretrained_model_name_or_path) / "language_model.bin"
            roberta.model = RobertaModel.from_pretrained(farm_lm_model, config=config, **kwargs)
            roberta.language = roberta.model.config.language
        else:
            # Huggingface transformer Style
            roberta.model = RobertaModel.from_pretrained(str(pretrained_model_name_or_path), **kwargs)
            roberta.language = cls._get_or_infer_language_from_name(language, pretrained_model_name_or_path)
        return roberta

    def forward(
        self,
        input_ids,
        segment_ids,
        padding_mask,
        **kwargs,
    ):
        """
        Perform the forward pass of the Roberta model.

        :param input_ids: The ids of each token in the input sequence. Is a tensor of shape [batch_size, max_seq_len]
        :type input_ids: torch.Tensor
        :param segment_ids: The id of the segment. For example, in next sentence prediction, the tokens in the
           first sentence are marked with 0 and those in the second are marked with 1.
           It is a tensor of shape [batch_size, max_seq_len]
        :type segment_ids: torch.Tensor
        :param padding_mask: A mask that assigns a 1 to valid input tokens and 0 to padding tokens
           of shape [batch_size, max_seq_len]
        :return: Embeddings for each token in the input sequence.

        """
        output_tuple = self.model(
            input_ids,
            token_type_ids=segment_ids,
            attention_mask=padding_mask,
        )
        if self.model.encoder.output_hidden_states == True:
            sequence_output, pooled_output, all_hidden_states = output_tuple[0], output_tuple[1], output_tuple[2]
            return sequence_output, pooled_output, all_hidden_states
        else:
            sequence_output, pooled_output = output_tuple[0], output_tuple[1]
            return sequence_output, pooled_output

    def enable_hidden_states_output(self):
        self.model.encoder.output_hidden_states = True

    def disable_hidden_states_output(self):
        self.model.encoder.output_hidden_states = False


class XLMRoberta(LanguageModel):
    """
    A roberta model that wraps the HuggingFace's implementation
    (https://github.com/huggingface/transformers) to fit the LanguageModel class.
    Paper: https://arxiv.org/abs/1907.11692

    """

    def __init__(self):
        super(XLMRoberta, self).__init__()
        self.model = None
        self.name = "xlm_roberta"

    @classmethod
    def load(cls, pretrained_model_name_or_path, language=None, **kwargs):
        """
        Load a language model either by supplying

        * the name of a remote model on s3 ("xlm-roberta-base" ...)
        * or a local path of a model trained via transformers ("some_dir/huggingface_model")
        * or a local path of a model trained via FARM ("some_dir/farm_model")

        :param pretrained_model_name_or_path: name or path of a model
        :param language: (Optional) Name of language the model was trained for (e.g. "german").
                         If not supplied, FARM will try to infer it from the model name.
        :return: Language Model

        """
        xlm_roberta = cls()
        if "farm_lm_name" in kwargs:
            xlm_roberta.name = kwargs["farm_lm_name"]
        else:
            xlm_roberta.name = pretrained_model_name_or_path
        # We need to differentiate between loading model using FARM format and Pytorch-Transformers format
        farm_lm_config = Path(pretrained_model_name_or_path) / "language_model_config.json"
        if os.path.exists(farm_lm_config):
            # FARM style
            config = XLMRobertaConfig.from_pretrained(farm_lm_config)
            farm_lm_model = Path(pretrained_model_name_or_path) / "language_model.bin"
            xlm_roberta.model = XLMRobertaModel.from_pretrained(farm_lm_model, config=config, **kwargs)
            xlm_roberta.language = xlm_roberta.model.config.language
        else:
            # Huggingface transformer Style
            xlm_roberta.model = XLMRobertaModel.from_pretrained(str(pretrained_model_name_or_path), **kwargs)
            xlm_roberta.language = cls._get_or_infer_language_from_name(language, pretrained_model_name_or_path)
        return xlm_roberta

    def forward(
        self,
        input_ids,
        segment_ids,
        padding_mask,
        **kwargs,
    ):
        """
        Perform the forward pass of the XLMRoberta model.

        :param input_ids: The ids of each token in the input sequence. Is a tensor of shape [batch_size, max_seq_len]
        :type input_ids: torch.Tensor
        :param segment_ids: The id of the segment. For example, in next sentence prediction, the tokens in the
           first sentence are marked with 0 and those in the second are marked with 1.
           It is a tensor of shape [batch_size, max_seq_len]
        :type segment_ids: torch.Tensor
        :param padding_mask: A mask that assigns a 1 to valid input tokens and 0 to padding tokens
           of shape [batch_size, max_seq_len]
        :return: Embeddings for each token in the input sequence.

        """
        output_tuple = self.model(
            input_ids,
            token_type_ids=segment_ids,
            attention_mask=padding_mask,
        )
        if self.model.encoder.output_hidden_states == True:
            sequence_output, pooled_output, all_hidden_states = output_tuple[0], output_tuple[1], output_tuple[2]
            return sequence_output, pooled_output, all_hidden_states
        else:
            sequence_output, pooled_output = output_tuple[0], output_tuple[1]
            return sequence_output, pooled_output

    def enable_hidden_states_output(self):
        self.model.encoder.output_hidden_states = True

    def disable_hidden_states_output(self):
        self.model.encoder.output_hidden_states = False


class DistilBert(LanguageModel):
    """
    A DistilBERT model that wraps HuggingFace's implementation
    (https://github.com/huggingface/transformers) to fit the LanguageModel class.

    NOTE: 
    - DistilBert doesn’t have token_type_ids, you don’t need to indicate which 
    token belongs to which segment. Just separate your segments with the separation 
    token tokenizer.sep_token (or [SEP])
    - Unlike the other BERT variants, DistilBert does not output the
    pooled_output. An additional pooler is initialized.
    
    """

    def __init__(self):
        super(DistilBert, self).__init__()
        self.model = None
        self.name = "distilbert"
        self.pooler = None

    @classmethod
    def load(cls, pretrained_model_name_or_path, language=None, **kwargs):
        """
        Load a pretrained model by supplying

        * the name of a remote model on s3 ("distilbert-base-german-cased" ...)
        * OR a local path of a model trained via transformers ("some_dir/huggingface_model")
        * OR a local path of a model trained via FARM ("some_dir/farm_model")

        :param pretrained_model_name_or_path: The path of the saved pretrained model or its name.
        :type pretrained_model_name_or_path: str

        """

        distilbert = cls()
        if "farm_lm_name" in kwargs:
            distilbert.name = kwargs["farm_lm_name"]
        else:
            distilbert.name = pretrained_model_name_or_path
        # We need to differentiate between loading model using FARM format and Pytorch-Transformers format
        farm_lm_config = Path(pretrained_model_name_or_path) / "language_model_config.json"
        if os.path.exists(farm_lm_config):
            # FARM style
            config = AlbertConfig.from_pretrained(farm_lm_config)
            farm_lm_model = Path(pretrained_model_name_or_path) / "language_model.bin"
            distilbert.model = DistilBertModel.from_pretrained(farm_lm_model, config=config, **kwargs)
            distilbert.language = distilbert.model.config.language
        else:
            # Pytorch-transformer Style
            distilbert.model = DistilBertModel.from_pretrained(str(pretrained_model_name_or_path), **kwargs)
            distilbert.language = cls._get_or_infer_language_from_name(language, pretrained_model_name_or_path)
        config = distilbert.model.config

        # DistilBERT does not provide a pooled_output by default. Therefore, we need to initialize an extra pooler.
        # The pooler takes the first hidden representation & feeds it to a dense layer of (hidden_dim x hidden_dim).
        # We don't want a dropout in the end of the pooler, since we do that already in the adaptive model before we
        # feed everything to the prediction head
        config.summary_last_dropout = 0
        config.summary_type = 'first'
        config.summary_activation = 'tanh'
        distilbert.pooler = SequenceSummary(config)
        distilbert.pooler.apply(distilbert.model._init_weights)
        return distilbert

    def forward(
        self,
        input_ids,
        padding_mask,
        **kwargs,
    ):
        """
        Perform the forward pass of the DistilBERT model.

        :param input_ids: The ids of each token in the input sequence. Is a tensor of shape [batch_size, max_seq_len]
        :type input_ids: torch.Tensor
        :param padding_mask: A mask that assigns a 1 to valid input tokens and 0 to padding tokens
           of shape [batch_size, max_seq_len]
        :return: Embeddings for each token in the input sequence.

        """
        output_tuple = self.model(
            input_ids,
            attention_mask=padding_mask,
        )
        # We need to manually aggregate that to get a pooled output (one vec per seq)
        pooled_output = self.pooler(output_tuple[0])
        if self.model.config.output_hidden_states == True:
            sequence_output, all_hidden_states = output_tuple[0], output_tuple[1]
            return sequence_output, pooled_output
        else:
            sequence_output = output_tuple[0]
            return sequence_output, pooled_output

    def enable_hidden_states_output(self):
        self.model.config.output_hidden_states = True

    def disable_hidden_states_output(self):
        self.model.config.output_hidden_states = False


class XLNet(LanguageModel):
    """
    A XLNet model that wraps the HuggingFace's implementation
    (https://github.com/huggingface/transformers) to fit the LanguageModel class.
    Paper: https://arxiv.org/abs/1906.08237
    """

    def __init__(self):
        super(XLNet, self).__init__()
        self.model = None
        self.name = "xlnet"
        self.pooler = None

    @classmethod
    def load(cls, pretrained_model_name_or_path, language=None, **kwargs):
        """
        Load a language model either by supplying

        * the name of a remote model on s3 ("xlnet-base-cased" ...)
        * or a local path of a model trained via transformers ("some_dir/huggingface_model")
        * or a local path of a model trained via FARM ("some_dir/farm_model")

        :param pretrained_model_name_or_path: name or path of a model
        :param language: (Optional) Name of language the model was trained for (e.g. "german").
                         If not supplied, FARM will try to infer it from the model name.
        :return: Language Model

        """
        xlnet = cls()
        if "farm_lm_name" in kwargs:
            xlnet.name = kwargs["farm_lm_name"]
        else:
            xlnet.name = pretrained_model_name_or_path
        # We need to differentiate between loading model using FARM format and Pytorch-Transformers format
        farm_lm_config = Path(pretrained_model_name_or_path) / "language_model_config.json"
        if os.path.exists(farm_lm_config):
            # FARM style
            config = XLNetConfig.from_pretrained(farm_lm_config)
            farm_lm_model = Path(pretrained_model_name_or_path) / "language_model.bin"
            xlnet.model = XLNetModel.from_pretrained(farm_lm_model, config=config, **kwargs)
            xlnet.language = xlnet.model.config.language
        else:
            # Pytorch-transformer Style
            xlnet.model = XLNetModel.from_pretrained(str(pretrained_model_name_or_path), **kwargs)
            xlnet.language = cls._get_or_infer_language_from_name(language, pretrained_model_name_or_path)
            config = xlnet.model.config
        # XLNet does not provide a pooled_output by default. Therefore, we need to initialize an extra pooler.
        # The pooler takes the last hidden representation & feeds it to a dense layer of (hidden_dim x hidden_dim).
        # We don't want a dropout in the end of the pooler, since we do that already in the adaptive model before we
        # feed everything to the prediction head
        config.summary_last_dropout = 0
        xlnet.pooler = SequenceSummary(config)
        xlnet.pooler.apply(xlnet.model._init_weights)
        return xlnet

    def forward(
        self,
        input_ids,
        segment_ids,
        padding_mask,
        **kwargs,
    ):
        """
        Perform the forward pass of the XLNet model.

        :param input_ids: The ids of each token in the input sequence. Is a tensor of shape [batch_size, max_seq_len]
        :type input_ids: torch.Tensor
        :param segment_ids: The id of the segment. For example, in next sentence prediction, the tokens in the
           first sentence are marked with 0 and those in the second are marked with 1.
           It is a tensor of shape [batch_size, max_seq_len]
        :type segment_ids: torch.Tensor
        :param padding_mask: A mask that assigns a 1 to valid input tokens and 0 to padding tokens
           of shape [batch_size, max_seq_len]
        :return: Embeddings for each token in the input sequence.
        """

        # Note: XLNet has a couple of special input tensors for pretraining / text generation  (perm_mask, target_mapping ...)
        # We will need to implement them, if we wanna support LM adaptation

        output_tuple = self.model(
            input_ids,
            token_type_ids=segment_ids,
            attention_mask=padding_mask,
        )
        # XLNet also only returns the sequence_output (one vec per token)
        # We need to manually aggregate that to get a pooled output (one vec per seq)
        #TODO verify that this is really doing correct pooling
        pooled_output = self.pooler(output_tuple[0])

        if self.model.output_hidden_states == True:
            sequence_output, all_hidden_states = output_tuple[0], output_tuple[1]
            return sequence_output, pooled_output, all_hidden_states
        else:
            sequence_output = output_tuple[0]
            return sequence_output, pooled_output

    def enable_hidden_states_output(self):
        self.model.output_hidden_states = True

    def disable_hidden_states_output(self):
        self.model.output_hidden_states = False

class EmbeddingConfig():
    def __init__(self,
                 name=None,
                 embeddings_filename=None,
                 vocab_filename=None,
                 vocab_size=None,
                 hidden_size=None,
                 language=None,
                 **kwargs):
        self.name = name
        self.embeddings_filename = embeddings_filename
        self.vocab_filename = vocab_filename
        self.vocab_size = vocab_size
        self.hidden_size = hidden_size
        self.language = language

    def to_dict(self):
        """
        Serializes this instance to a Python dictionary.

        Returns:
            :obj:`Dict[str, any]`: Dictionary of all the attributes that make up this configuration instance,
        """
        output = copy.deepcopy(self.__dict__)
        if hasattr(self.__class__, "model_type"):
            output["model_type"] = self.__class__.model_type
        return output

    def to_json_string(self):
        """
        Serializes this instance to a JSON string.

        Returns:
            :obj:`string`: String containing all the attributes that make up this configuration instance in JSON format.
        """
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"




class EmbeddingModel():
    def __init__(self, path, config, vocab_filename):
        super(EmbeddingModel, self).__init__()
        self.config = EmbeddingConfig(**dict(config))
        self.vocab = load_vocab(vocab_filename)
        self.embeddings = self.load_vectors(path=path)
        assert "[UNK]" in self.vocab, "No [UNK] symbol in Wordembeddingmodel! Aborting"
        self.unk_idx = self.vocab["[UNK]"]

    def save(self,save_dir):
        # Save Weights
        save_name = Path(save_dir) / self.config.embeddings_filename
        with open(save_name, "w") as f:
            for w, vec in zip(self.vocab, self.embeddings):
                f.write(w + " " + " ".join(["%.6f" % v for v in vec]) + "\n")
        f.close()

    def load_vectors(self,path):

        f = io.open(path, 'rt', encoding='utf-8').readlines()

        words_transformed = set()
        repetitions = 0
        embeddings_dimensionality = None
        vectors = {}

        for line in tqdm(f):
            line = line.strip()
            if line:
                word, vec = line.split(' ', 1)
                if (word not in words_transformed):  # omit repetitions = speed up + debug
                    try:
                        np_vec = np.fromstring(vec, sep=' ')
                        if embeddings_dimensionality is None:
                            if len(np_vec) < 4:  # word2vec includes number of vectors and its dimension as header
                                logger.info("Skipping header")
                                continue
                            else:
                                embeddings_dimensionality = len(np_vec)
                        if len(np_vec) == embeddings_dimensionality:
                            vectors[word] = np_vec
                            words_transformed.add(word)
                    except:
                        if logger is not None:
                            logger.debug("Embeddings reader: Could not convert line: {}".format(line))
                else:
                    repetitions += 1


        embeddings = torch.zeros((len(self.vocab),embeddings_dimensionality)) # TODO random init of all embeddings, so if it isnt filled it can still learn
        for i, w in enumerate(self.vocab):
            current = vectors.get(w,np.zeros(embeddings_dimensionality))
            if w not in vectors:
                logger.warning(f"Could not load pretrained embedding for word: {w}")
            embeddings[i,:] = torch.tensor(current)
        return embeddings

    def resize_token_embeddings(self, new_num_tokens=None):
        # hacky way of returning an object with num_embeddings attribute set
        # TODO add functionality to add words/tokens to a wordembeddingmodel after initialization
        temp = {}
        temp["num_embeddings"] = len(self.vocab)
        temp = DotMap(temp)
        return temp



class WordEmbedding_LM(LanguageModel):
    """
    A wrapper around facebooks fasttext https://github.com/facebookresearch/fastText/
     to fit the LanguageModel class.

    NOTE:
    - since fasttext just maps words to embeddings, we can not apply gradients to fasttext directly
    - Unlike the other LM variants, fasttext does not output the
    pooled_output. An additional pooler is initialized.

    """

    def __init__(self):
        super(WordEmbedding_LM, self).__init__()
        self.model = None
        self.name = "WordEmbedding_LM"
        self.pooler = None


    @classmethod
    def load(cls, pretrained_model_name_or_path, language=None, **kwargs):
        """
        Load a language model either by supplying

        * a local path of a model trained via FARM ("some_dir/farm_model")
        * the name of a remote model on s3
        * TODO: or a local path of a model trained via transformers (NOT SUPPORTED)

        :param pretrained_model_name_or_path: name or path of a model
        :param language: (Optional) Name of language the model was trained for (e.g. "german").
                         If not supplied, FARM will try to infer it from the model name.
        :return: Language Model

        """
        import fasttext
        wordembedding_LM = cls()
        if "farm_lm_name" in kwargs:
            wordembedding_LM.name = kwargs["farm_lm_name"]
        else:
            wordembedding_LM.name = pretrained_model_name_or_path
        # We need to differentiate between loading model using FARM format and Pytorch-Transformers format
        farm_lm_config = Path(pretrained_model_name_or_path) / "language_model_config.json"
        if os.path.exists(farm_lm_config):
            # FARM style
            config = json.load(open(farm_lm_config,"r"))
            farm_lm_model = Path(pretrained_model_name_or_path) / config["embeddings_filename"]
            vocab_filename = Path(pretrained_model_name_or_path) / config["vocab_filename"]
            wordembedding_LM.model = EmbeddingModel(path=str(farm_lm_model), config=config, vocab_filename=str(vocab_filename))
            wordembedding_LM.language = config.get("language", None)
        else:
            raise NotImplementedError
            #load_config(pretrained_model_name_or_path)


        # taking the mean for getting the pooled representation
        # TODO: extend this to other pooling operations or remove completely
        wordembedding_LM.pooler = lambda x: torch.mean(x, dim=0)
        return wordembedding_LM


    def load_config(self, pretrained_model_name_or_path):

        from transformers.file_utils import cached_path
        from transformers import configuration_utils
        config_file = PRETRAINED_CONFIG_ARCHIVE_MAP[pretrained_model_name_or_path]

        try:
            # Load from URL or cache if already cached
            resolved_config_file = cached_path(
                config_file,
            )
            # Load config dict
            if resolved_config_file is None:
                raise EnvironmentError
            config_dict = configuration_utils._dict_from_json_file(resolved_config_file)

        except EnvironmentError:
            if pretrained_model_name_or_path in PRETRAINED_CONFIG_ARCHIVE_MAP:
                msg = "Couldn't reach server at '{}' to download pretrained model configuration file.".format(
                    config_file
                )
            else:
                msg = (
                    "Model name '{}' was not found in model name list. "
                    "We assumed '{}' was a path, a model identifier, or url to a configuration file or "
                    "a directory containing such a file but couldn't find any such file at this path or url.".format(
                        pretrained_model_name_or_path, config_file,
                    )
                )
            raise EnvironmentError(msg)
        return config_dict

    def save(self, save_dir):
        """
        Save the model embeddings and its config file so that it can be loaded again.
        #TODO make embeddings trainable and save trained embeddings
        :param save_dir: The directory in which the model should be saved.
        :type save_dir: str
        """
        #save model
        self.model.save(save_dir=save_dir)
        #save config
        self.save_config(save_dir=save_dir)


    def forward(self, input_ids, **kwargs,):
        """
        Perform the forward pass of the fasttext model.
        This is just the mapping of words to their corresponding (and aggregated) n-gram embeddings
        """
        sequence_output = []
        pooled_output = []
        for sample in input_ids:
            sample_embeddings = []
            for index in sample:
                #if index != self.model.unk_idx:
                sample_embeddings.append(self.model.embeddings[index])
            sample_embeddings = torch.stack(sample_embeddings)
            sequence_output.append(sample_embeddings)
            pooled_output.append(self.pooler(sample_embeddings))

        #pooled_output = torch.stack([torch.tensor(x) for x in pooled_output])
        sequence_output = torch.stack(sequence_output)
        pooled_output = torch.stack(pooled_output)
        m = nn.BatchNorm1d(pooled_output.shape[1])
        pooled_output = m(pooled_output)
        return sequence_output, pooled_output
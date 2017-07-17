# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not
# use this file except in compliance with the License. A copy of the License
# is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

"""
Simple Training CLI.
"""
import argparse
import json
import os
import pickle
import random
import shutil
import sys
from contextlib import ExitStack
from typing import Optional, Dict

import mxnet as mx
import numpy as np

from sockeye.log import setup_main_logger, log_sockeye_version
from sockeye.utils import acquire_gpus, check_condition, get_num_gpus, expand_requested_device_ids
from . import arguments
from . import attention
from . import constants as C
from . import coverage
from . import data_io
from . import decoder
from . import encoder
from . import initializer
from . import lexicon
from . import loss
from . import lr_scheduler
from . import model
from . import rnn
from . import training
from . import vocab


def none_if_negative(val):
    return None if val < 0 else val


def _build_or_load_vocab(existing_vocab_path: Optional[str], data_path: str, num_words: int,
                         word_min_count: int) -> Dict:
    if existing_vocab_path is None:
        vocabulary = vocab.build_from_path(data_path,
                                           num_words=num_words,
                                           min_count=word_min_count)
    else:
        vocabulary = vocab.vocab_from_json(existing_vocab_path)
    return vocabulary


def _dict_difference(dict1: Dict, dict2: Dict):
    diffs = set()
    for k, v in dict1.items():
        if k not in dict2 or dict2[k] != v:
            diffs.add(k)
    return diffs


def main():
    params = argparse.ArgumentParser(description='CLI to train sockeye sequence-to-sequence models.')
    arguments.add_io_args(params)
    arguments.add_model_parameters(params)
    arguments.add_training_args(params)
    arguments.add_device_args(params)
    args = params.parse_args()

    # seed the RNGs
    np.random.seed(args.seed)
    random.seed(args.seed)
    mx.random.seed(args.seed)

    if args.use_fused_rnn:
        check_condition(not args.use_cpu, "GPU required for FusedRNN cells")

    if args.rnn_residual_connections:
        check_condition(args.rnn_num_layers > 2, "Residual connections require at least 3 RNN layers")

    check_condition(args.optimized_metric == C.BLEU or args.optimized_metric in args.metrics,
                    "Must optimize either BLEU or one of tracked metrics (--metrics)")

    # Checking status of output folder, resumption, etc.
    # Create temporary logger to console only
    logger = setup_main_logger(__name__, file_logging=False, console=not args.quiet)
    output_folder = os.path.abspath(args.output)
    resume_training = False
    training_state_dir = os.path.join(output_folder, C.TRAINING_STATE_DIRNAME)
    if os.path.exists(output_folder):
        if args.overwrite_output:
            logger.info("Removing existing output folder %s.", output_folder)
            shutil.rmtree(output_folder)
            os.makedirs(output_folder)
        elif os.path.exists(training_state_dir):
            with open(os.path.join(output_folder, C.ARGS_STATE_NAME), "r") as fp:
                old_args = json.load(fp)
            arg_diffs = _dict_difference(vars(args), old_args) | _dict_difference(old_args, vars(args))
            # Remove args that may differ without affecting the training.
            arg_diffs -= set(C.ARGS_MAY_DIFFER)
            # allow different device-ids provided their total count is the same
            if 'device_ids' in arg_diffs and len(old_args['device_ids']) == len(vars(args)['device_ids']):
                arg_diffs.discard('device_ids')
            if not arg_diffs:
                resume_training = True
            else:
                # We do not have the logger yet
                logger.error("Mismatch in arguments for training continuation.")
                logger.error("Differing arguments: %s.", ", ".join(arg_diffs))
                sys.exit(1)
        else:
            logger.error("Refusing to overwrite existing output folder %s.", output_folder)
            sys.exit(1)
    else:
        os.makedirs(output_folder)

    logger = setup_main_logger(__name__,
                               file_logging=True,
                               console=not args.quiet, path=os.path.join(output_folder, C.LOG_NAME))
    log_sockeye_version(logger)
    logger.info("Command: %s", " ".join(sys.argv))
    logger.info("Arguments: %s", args)
    with open(os.path.join(output_folder, C.ARGS_STATE_NAME), "w") as fp:
        json.dump(vars(args), fp)

    with ExitStack() as exit_stack:
        # context
        if args.use_cpu:
            logger.info("Device: CPU")
            context = [mx.cpu()]
        else:
            num_gpus = get_num_gpus()
            check_condition(num_gpus >= 1,
                            "No GPUs found, consider running on the CPU with --use-cpu "
                            "(note: check depends on nvidia-smi and this could also mean that the nvidia-smi "
                            "binary isn't on the path).")
            if args.disable_device_locking:
                context = expand_requested_device_ids(args.device_ids)
            else:
                context = exit_stack.enter_context(acquire_gpus(args.device_ids, lock_dir=args.lock_dir))
            logger.info("Device(s): GPU %s", context)
            context = [mx.gpu(gpu_id) for gpu_id in context]

        # load existing or create vocabs
        if resume_training:
            vocab_source = vocab.vocab_from_json_or_pickle(os.path.join(output_folder, C.VOCAB_SRC_NAME))
            vocab_target = vocab.vocab_from_json_or_pickle(os.path.join(output_folder, C.VOCAB_TRG_NAME))
        else:
            num_words_source = args.num_words if args.num_words_source is None else args.num_words_source
            vocab_source = _build_or_load_vocab(args.source_vocab, args.source, num_words_source, args.word_min_count)
            vocab.vocab_to_json(vocab_source, os.path.join(output_folder, C.VOCAB_SRC_NAME) + C.JSON_SUFFIX)

            num_words_target = args.num_words if args.num_words_target is None else args.num_words_target
            vocab_target = _build_or_load_vocab(args.target_vocab, args.target, num_words_target, args.word_min_count)
            vocab.vocab_to_json(vocab_target, os.path.join(output_folder, C.VOCAB_TRG_NAME) + C.JSON_SUFFIX)

        vocab_source_size = len(vocab_source)
        vocab_target_size = len(vocab_target)
        logger.info("Vocabulary sizes: source=%d target=%d", vocab_source_size, vocab_target_size)

        data_info = data_io.DataInfo(os.path.abspath(args.source),
                                     os.path.abspath(args.target),
                                     os.path.abspath(args.validation_source),
                                     os.path.abspath(args.validation_target),
                                     args.source_vocab,
                                     args.target_vocab)

        # create data iterators
        max_seq_len_source = args.max_seq_len if args.max_seq_len_source is None else args.max_seq_len_source
        max_seq_len_target = args.max_seq_len if args.max_seq_len_target is None else args.max_seq_len_target
        train_iter, eval_iter = data_io.get_training_data_iters(source=data_info.source,
                                                                target=data_info.target,
                                                                validation_source=data_info.validation_source,
                                                                validation_target=data_info.validation_target,
                                                                vocab_source=vocab_source,
                                                                vocab_target=vocab_target,
                                                                batch_size=args.batch_size,
                                                                fill_up=args.fill_up,
                                                                max_seq_len_source=max_seq_len_source,
                                                                max_seq_len_target=max_seq_len_target,
                                                                bucketing=not args.no_bucketing,
                                                                bucket_width=args.bucket_width)

        # learning rate scheduling
        learning_rate_half_life = none_if_negative(args.learning_rate_half_life)
        # TODO: The loading for continuation of the scheduler is done separately from the other parts
        if not resume_training:
            lr_scheduler_instance = lr_scheduler.get_lr_scheduler(args.learning_rate_scheduler_type,
                                                                  args.checkpoint_frequency,
                                                                  learning_rate_half_life,
                                                                  args.learning_rate_reduce_factor,
                                                                  args.learning_rate_reduce_num_not_improved)
        else:
            with open(os.path.join(training_state_dir, C.SCHEDULER_STATE_NAME), "rb") as fp:
                lr_scheduler_instance = pickle.load(fp)

        # model configuration
        num_embed_source = args.num_embed if args.num_embed_source is None else args.num_embed_source
        num_embed_target = args.num_embed if args.num_embed_target is None else args.num_embed_target

        config_rnn = rnn.RNNConfig(cell_type=args.rnn_cell_type,
                                   num_hidden=args.rnn_num_hidden,
                                   num_layers=args.rnn_num_layers,
                                   dropout=args.dropout,
                                   residual=args.rnn_residual_connections,
                                   forget_bias=args.rnn_forget_bias)

        config_conv = None
        if args.encoder == C.RNN_WITH_CONV_EMBED_NAME:
            config_conv = encoder.ConvolutionalEmbeddingConfig(num_embed=num_embed_source,
                                                               max_filter_width=args.conv_embed_max_filter_width,
                                                               num_filters=args.conv_embed_num_filters,
                                                               pool_stride=args.conv_embed_pool_stride,
                                                               num_highway_layers=args.conv_embed_num_highway_layers,
                                                               dropout=args.dropout)

        config_encoder = encoder.RecurrentEncoderConfig(vocab_size=vocab_source_size,
                                                        num_embed=num_embed_source,
                                                        rnn_config=config_rnn,
                                                        conv_config=config_conv)

        config_decoder = decoder.RecurrentDecoderConfig(vocab_size=vocab_target_size,
                                                        num_embed=num_embed_target,
                                                        rnn_config=config_rnn,
                                                        dropout=args.dropout,
                                                        weight_tying=args.weight_tying,
                                                        context_gating=args.context_gating,
                                                        layer_normalization=args.layer_normalization)

        attention_num_hidden = args.rnn_num_hidden if not args.attention_num_hidden else args.attention_num_hidden
        config_coverage = None
        if args.attention_type == "coverage":
            config_coverage = coverage.CoverageConfig(type=args.attention_coverage_type,
                                                      num_hidden=args.attention_coverage_num_hidden,
                                                      layer_normalization=args.layer_normalization)
        config_attention = attention.AttentionConfig(type=args.attention_type,
                                                     num_hidden=attention_num_hidden,
                                                     input_previous_word=args.attention_use_prev_word,
                                                     rnn_num_hidden=config_rnn.num_hidden,
                                                     layer_normalization=args.layer_normalization,
                                                     config_coverage=config_coverage)

        config_loss = loss.LossConfig(type=args.loss,
                                      vocab_size=vocab_target_size,
                                      normalize=args.normalize_loss,
                                      smoothed_cross_entropy_alpha=args.smoothed_cross_entropy_alpha)

        model_config = model.ModelConfig(max_seq_len=max_seq_len_source,
                                         vocab_source_size=vocab_source_size,
                                         vocab_target_size=vocab_target_size,
                                         config_encoder=config_encoder,
                                         config_decoder=config_decoder,
                                         config_attention=config_attention,
                                         config_loss=config_loss,
                                         lexical_bias=args.lexical_bias,
                                         learn_lexical_bias=args.learn_lexical_bias)
        model_config.freeze()

        # create training model
        training_model = training.TrainingModel(config=model_config,
                                                context=context,
                                                train_iter=train_iter,
                                                fused=args.use_fused_rnn,
                                                bucketing=not args.no_bucketing,
                                                lr_scheduler=lr_scheduler_instance)

        # We may consider loading the params in TrainingModule, for consistency
        # with the training state saving
        if resume_training:
            logger.info("Found partial training in directory %s. Resuming from saved state.", training_state_dir)
            training_model.load_params_from_file(os.path.join(training_state_dir, C.TRAINING_STATE_PARAMS_NAME))
        elif args.params:
            logger.info("Training will initialize from parameters loaded from '%s'", args.params)
            training_model.load_params_from_file(args.params)

        lexicon_array = lexicon.initialize_lexicon(args.lexical_bias,
                                                   vocab_source, vocab_target) if args.lexical_bias else None

        weight_initializer = initializer.get_initializer(args.rnn_h2h_init, lexicon=lexicon_array)

        optimizer = args.optimizer
        optimizer_params = {'wd': args.weight_decay,
                            "learning_rate": args.initial_learning_rate}
        if lr_scheduler_instance is not None:
            optimizer_params["lr_scheduler"] = lr_scheduler_instance
        clip_gradient = none_if_negative(args.clip_gradient)
        if clip_gradient is not None:
            optimizer_params["clip_gradient"] = clip_gradient
        if args.momentum is not None:
            optimizer_params["momentum"] = args.momentum
        if args.normalize_loss:
            # When normalize_loss is turned on we normalize by the number of non-PAD symbols in a batch which implicitly
            # already contains the number of sentences and therefore we need to disable rescale_grad.
            optimizer_params["rescale_grad"] = 1.0
        else:
            # Making MXNet module API's default scaling factor explicit
            optimizer_params["rescale_grad"] = 1.0 / args.batch_size
        logger.info("Optimizer: %s", optimizer)
        logger.info("Optimizer Parameters: %s", optimizer_params)

        training_model.fit(train_iter, eval_iter,
                           output_folder=output_folder,
                           max_params_files_to_keep=args.keep_last_params,
                           metrics=args.metrics,
                           initializer=weight_initializer,
                           max_updates=args.max_updates,
                           checkpoint_frequency=args.checkpoint_frequency,
                           optimizer=optimizer, optimizer_params=optimizer_params,
                           optimized_metric=args.optimized_metric,
                           max_num_not_improved=args.max_num_checkpoint_not_improved,
                           min_num_epochs=args.min_num_epochs,
                           monitor_bleu=args.monitor_bleu,
                           use_tensorboard=args.use_tensorboard)


if __name__ == "__main__":
    main()

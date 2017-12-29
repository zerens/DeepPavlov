"""
Copyright 2017 Neural Networks and Deep Learning lab, MIPT

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import re
from pathlib import Path

import numpy as np
from typing import Type

from deeppavlov.core.common import paths
from deeppavlov.core.common.registry import register
from deeppavlov.core.data.utils import load_vocab
from deeppavlov.core.models.inferable import Inferable
from deeppavlov.core.models.trainable import Trainable
# from deeppavlov.models.embedders.fasttext_embedder import FasttextUtteranceEmbed
from deeppavlov.models.embedders.w2v_embedder import Word2VecEmbedder
from deeppavlov.models.encoders.bow import BoW_encoder
from deeppavlov.models.ner.slotfill import DstcSlotFillingNetwork
from deeppavlov.models.tokenizers.spacy_tokenizer import SpacyTokenizer
from deeppavlov.models.trackers.default_tracker import DefaultTracker
from deeppavlov.skills.hcn_new.metrics import DialogMetrics
from deeppavlov.skills.hcn_new.network import HybridCodeNetworkModel
from deeppavlov.skills.hcn_new.templates import Templates, DualTemplate


@register("hcn_new")
class HybridCodeNetworkBot(Inferable, Trainable):
    def __init__(self, template_path, slot_names,
                 template_type: Type = DualTemplate,
                 slot_filler: Type = DstcSlotFillingNetwork,
                 bow_encoder: Type = BoW_encoder,
                 embedder: Type = Word2VecEmbedder,
                 tokenizer: Type = SpacyTokenizer,
                 tracker: Type = DefaultTracker,
                 network: Type = HybridCodeNetworkModel,
                 vocab_path=None,
                 use_action_mask=False,
                 debug=False):

        self.episode_done = True
        self.use_action_mask = use_action_mask
        self.debug = debug
        # TODO: infer slot names from dataset
        self.slot_names = slot_names
        self.slot_filler = slot_filler
        self.bow_encoder = bow_encoder
        self.embedder = embedder
        self.tokenizer = tokenizer
        self.tracker = tracker
        self.network = network

        if vocab_path is None:
            vocab_path = Path(paths.USR_PATH).joinpath('vocab.txt')

        self.vocab = load_vocab(vocab_path)

        self.templates = Templates(template_type).load(template_path)
        print("[using {} templates from `{}`]" \
              .format(len(self.templates), template_path))

        # intialize parameters
        self.db_result = None
        self.n_actions = len(self.templates)
        self.prev_action = np.zeros(self.n_actions, dtype=np.float32)

        # initialize metrics
        self.metrics = DialogMetrics(self.n_actions)

        # opt = {
        #    'action_size': self.n_actions,
        #    'obs_size': 4 + len(self.vocab) + self.embedder.dim +\
        #    2 * self.tracker.state_size + self.n_actions
        # }
        # self.network = HybridCodeNetworkModel(opt)

    def _encode_context(self, context, db_result=None):
        # tokenize input
        tokenized = ' '.join(self.tokenizer.infer(context)).strip()
        if self.debug:
            print("Text tokens = `{}`".format(tokenized))

        # Bag of words features
        bow_features = self.bow_encoder.infer(tokenized, self.vocab)
        bow_features = bow_features.astype(np.float32)

        # Embeddings
        emb_features = self.embedder.infer(tokenized)

        # Text entity features
        self.tracker.update_state(self.slot_filler.infer(tokenized))
        ent_features = self.tracker.infer()
        if self.debug:
            print("Found slots =", self.slot_filler.infer(tokenized))

        # Other features
        context_features = np.array([(db_result == {}) * 1.,
                                     (self.db_result == {}) * 1.],
                                    dtype=np.float32)

        return np.hstack((bow_features, emb_features, ent_features,
                          context_features, self.prev_action))[np.newaxis, :]

    def _encode_response(self, response, act):
        return self.templates.actions.index(act)

    def _decode_response(self, action_id):
        """
        Convert action template id and entities from tracker
        to final response.
        """
        template = self.templates.templates[int(action_id)]

        slots = self.tracker.get_state()
        if self.db_result is not None:
            for k, v in self.db_result.items():
                slots[k] = str(v)

        return template.generate_text(slots)

    def _action_mask(self):
        action_mask = np.ones(self.n_actions, dtype=np.float32)
        if self.use_action_mask:
            # TODO: non-ones action mask
            for a_id in range(self.n_actions):
                tmpl = str(self.templates.templates[a_id])
                for entity in re.findall('#{}', tmpl):
                    if entity not in self.tracker.get_state() \
                            and entity not in (self.db_result or {}):
                        action_mask[a_id] = 0
        return action_mask

    def train(self, data, num_epochs=40, acc_threshold=0.99):
        print('\n:: training started\n')

        for j in range(num_epochs):

            tr_data = data.iter_all('train')
            eval_data = data.iter_all('valid')

            self.reset_metrics()

            for context, response, other in tr_data:
                if other.get('episode_done'):
                    self.reset()
                    self.metrics.n_dialogs += 1

                if other.get('db_result') is not None:
                    self.db_result = other['db_result']
                action_id = self._encode_response(response, other['act'])

                loss, pred_id = self.network.train(
                    self._encode_context(context, other.get('db_result')),
                    action_id,
                    self._action_mask()
                )

                self.prev_action *= 0.
                self.prev_action[pred_id] = 1.

                pred = self._decode_response(pred_id).lower()
                true = self.tokenizer.infer(response.lower().split())

                # update metrics
                self.metrics.n_examples += 1
                self.metrics.train_loss += loss
                self.metrics.conf_matrix[pred_id, action_id] += 1
                self.metrics.n_corr_examples += int(pred == true)
                if self.debug and ((pred == true) != (pred_id == action_id)):
                    print("Slot filling problem: ")
                    print("Pred = {}: {}".format(pred_id, pred))
                    print("True = {}: {}".format(action_id, true))
                    print("State =", self.tracker.get_state())
                    print("db_result =", self.db_result)
                    # TODO: update dialog metrics
            print('\n\n:: {}.train {}'.format(j + 1, self.metrics.report()))

            metrics = self.evaluate(eval_data)
            print(':: {}.valid {}'.format(j + 1, metrics.report()))

            if metrics.action_accuracy > acc_threshold:
                print('Accuracy is {}, stopped training.' \
                      .format(metrics.action_accuracy))
                break
        self.save()

    def infer(self, context, db_result=None):
        if db_result is not None:
            self.db_result = db_result
        probs, pred_id = self.network.infer(
            self._encode_context(context, db_result),
            self._action_mask()
        )
        self.prev_action *= 0.
        self.prev_action[pred_id] = 1.
        return self._decode_response(pred_id)

    def evaluate(self, eval_data):
        metrics = DialogMetrics(self.n_actions)

        for context, response, other in eval_data:

            if other.get('episode_done'):
                self.reset()
                metrics.n_dialogs += 1

            if other.get('db_result') is not None:
                self.db_result = other['db_result']

            probs, pred_id = self.network.infer(
                self._encode_context(context, other.get('db_result')),
                self._action_mask()
            )

            self.prev_action *= 0.
            self.prev_action[pred_id] = 1.

            pred = self._decode_response(pred_id).lower()
            true = self.tokenizer.infer(response.lower().split())

            # update metrics
            metrics.n_examples += 1
            action_id = self._encode_response(response, other['act'])
            metrics.conf_matrix[pred_id, action_id] += 1
            metrics.n_corr_examples += int(pred == true)
        return metrics

    def reset(self):
        self.tracker.reset_state()
        self.db_result = None
        self.prev_action = np.zeros(self.n_actions, dtype=np.float32)
        self.network.reset_state()

    def report(self):
        return self.metrics.report()

    def reset_metrics(self):
        self.metrics.reset()

    def save(self):
        """Save the parameters of the model to a file."""
        self.network.save()

    def shutdown(self):
        self.network.shutdown()
        self.slot_filler.shutdown()

    def load(self):
        pass

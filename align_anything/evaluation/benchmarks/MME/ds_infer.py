# Copyright 2024 PKU-Alignment Team. All Rights Reserved.
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
# ==============================================================================

import argparse
import os
import pickle
import re
from collections import defaultdict
from typing import Any, Dict, List

import torch
import torch.distributed as dist
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader, DistributedSampler
from tqdm import tqdm

from align_anything.evaluation.data_type import InferenceInput, InferenceOutput
from align_anything.evaluation.dataloader.base_dataloader import BaseDataLoader
from align_anything.evaluation.inference.ds_inference import (
    BaseInferencer_deepspeed,
    ListDataset,
    get_rank,
)
from align_anything.utils.device_utils import get_current_device, set_device
from align_anything.utils.template_registry import get_eval_template_class as get_template_class
from align_anything.utils.tools import (
    custom_cfgs_to_dict,
    dict_to_namedtuple,
    read_eval_cfgs,
    update_dict,
)
from datasets import DatasetDict, load_dataset


class MMEDataLoader(BaseDataLoader):

    def get_task_names(self):
        if isinstance(self.data_cfgs.task, list):
            return self.data_cfgs.task
        else:
            task_names = [self.data_cfgs.task]
            return task_names

    def get_answer(self, data):
        return data['answer']

    def build_example_prompt(self, data, with_answer=True):
        prompt = f"This is a {data['category']} problem.\n\n"
        return f"{prompt}{data['question']}"

    def build_prompt(self, data: Dict[str, Any]) -> str:
        assert self.num_shot == 0, 'MME does not support few-shot learning.'
        prompt = ''
        template = get_template_class(self.chat_template)
        question = [
            template.system_prompt
            + template.user_prompt.format(input=prompt + self.build_example_prompt(item, False))
            + template.assistant_prompt.format(output='')
            for item in data
        ]

        return question

    def preprocess(self, data):
        self.device = get_current_device()
        raw_images = [item['image'] for item in data]
        prompts = self.build_prompt(data)
        inputs = self.processor(prompts, raw_images, return_tensors='pt', padding=True)

        return prompts, inputs

    def load_dataset(self, category_datasets) -> DatasetDict:
        processed_inputs = {}
        for task, dataset in category_datasets.items():
            prompts, inputs = self.preprocess(dataset)
            processed_inputs[task] = []
            for prompt, input_ids, pixel_values, i in zip(
                prompts, inputs['input_ids'], inputs['pixel_values'], range(len(dataset))
            ):
                question_id, question = dataset[i]['question_id'], dataset[i]['question']
                processed_input = InferenceInput(
                    text=prompt, token_ids=input_ids, pixel_values=pixel_values
                )
                processed_input.question_id = question_id + question
                processed_inputs[task].append(processed_input)
        return processed_inputs

    def get_category_datasets(self):
        dataset = load_dataset(self.task_dir, 'default')[self.split]

        category_datasets = defaultdict(list)
        for i in tqdm(range(len(dataset)), desc='Dataset classification'):
            category = dataset[i]['category']
            if category in self.task_names:
                category_datasets[category].append(dataset[i])

        return category_datasets


class MMEGeneratorDS(BaseInferencer_deepspeed):
    def eval(
        self, data: Dict[str, List[InferenceInput]], eval_configs
    ) -> Dict[str, List[InferenceOutput]]:
        os.makedirs('.cache', exist_ok=True)
        uuid_path = f'.cache/{eval_configs.uuid}'
        os.makedirs(uuid_path, exist_ok=True)

        for task, input in data.items():
            task_dir = f'{uuid_path}/{task}'
            os.makedirs(task_dir, exist_ok=True)
            raw_output = self.generation(input)
            for item in raw_output:
                for i in range(len(item.response)):
                    item.response[i] = item.response[i][
                        len(re.sub('<image>', ' ', item.prompt, count=1)) :
                    ]
            self.save_pickle(raw_output, task_dir)

    def load_data_distributed(self, inputs: List[InferenceInput]) -> List[InferenceInput]:
        dataset = ListDataset(inputs)

        sampler = DistributedSampler(dataset) if torch.distributed.is_initialized() else None

        def collate_fn(batch):
            return {
                'pad_token_ids': pad_sequence(
                    [torch.tensor(b.token_ids) for b in batch],
                    batch_first=True,
                    padding_value=self.tokenizer.pad_token_id,
                ),
                'pixel_values': torch.stack([b.pixel_values for b in batch]),
                'token_ids': [b.token_ids for b in batch],
                'text': [b.text for b in batch],
                'question_id': [b.question_id for b in batch],
            }

        dataloader = DataLoader(
            dataset, sampler=sampler, batch_size=self.batch_size, collate_fn=collate_fn
        )
        return dataloader

    def _generation(self, inputs: List[InferenceInput]) -> List[InferenceOutput]:
        assert isinstance(inputs, list)

        num_sequences = 4
        dataloader = self.load_data_distributed(inputs)

        InferenceOutputs = []

        for batch in tqdm(dataloader):
            local_rank = int(os.environ['LOCAL_RANK'])
            outputs = self.model.generate(
                inputs=batch['pad_token_ids'].to(set_device(local_rank)),
                pixel_values=batch['pixel_values'].to(set_device(local_rank)),
                return_dict_in_generate=True,
                num_return_sequences=num_sequences,
                early_stopping=True,
                output_scores=True,
                num_beams=num_sequences,
                do_sample=True,
                max_new_tokens=1024,
            )
            transition_scores = self.model.compute_transition_scores(
                outputs['sequences'],
                outputs['scores'],
                normalize_logits=True,
                beam_indices=outputs['beam_indices'],
            )
            responses = self.processor.batch_decode(outputs['sequences'], skip_special_tokens=True)

            for i in range(self.batch_size):
                token_ids = batch['token_ids'][i]
                text = batch['text'][i]
                input_length = len(token_ids)
                response = responses[i * num_sequences : (i + 1) * num_sequences]
                output = outputs['sequences'][i * num_sequences : (i + 1) * num_sequences, :]
                transition_score = transition_scores[i * num_sequences : (i + 1) * num_sequences, :]
                inference_output = InferenceOutput.from_deepspeed_output(
                    deepspeed_output={
                        'prompt': text,
                        'prompt_token_ids': token_ids,
                        'prompt_logprobs': transition_score[:, :input_length],
                        'response': response,
                        'response_token_ids': output[:, input_length:],
                        'response_logprobs': transition_score[:, input_length:],
                        'raw_output': outputs[i * num_sequences : (i + 1) * num_sequences],
                    },
                    store_raw=True,
                )
                inference_output.question_id = batch['question_id'][i]
                InferenceOutputs.append(inference_output)
        return InferenceOutputs

    def save_pickle(self, output_data: List[InferenceOutput], task_dir: str = None):
        cache_data = []
        for item in output_data:
            cache_data.append(
                {
                    'question_id': item.question_id,
                    'prompt_text': item.prompt,
                    'response': item.response,
                }
            )
            if dist.is_initialized():
                file_path = f'{task_dir}/outputs_{get_rank()}.pkl'
            else:
                file_path = f'{task_dir}/outputs.pkl'

            with open(file_path, 'wb') as f:
                pickle.dump(cache_data, f, protocol=4)


def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    _, unparsed_args = parser.parse_known_args()
    keys = [k[2:] for k in unparsed_args[1::2]]
    values = list(unparsed_args[2::2])
    unparsed_args = dict(zip(keys, values))
    dict_configs, infer_configs = read_eval_cfgs('mme', 'deepspeed')
    for k, v in unparsed_args.items():
        if v == '' or v is None:
            continue
        dict_configs = update_dict(dict_configs, custom_cfgs_to_dict(k, v))
        infer_configs = update_dict(infer_configs, custom_cfgs_to_dict(k, v))
    dict_configs = dict_to_namedtuple(dict_configs)
    model_config = dict_configs.default.model_cfgs
    eval_configs = dict_configs.default.eval_cfgs
    dataloader = MMEDataLoader(dict_configs)
    dataset = dataloader.get_category_datasets()
    assert not (
        dataloader.num_shot > 0 or dataloader.cot
    ), 'Few-shot or chain-of-thought cannot be used for this benchmark.'
    test_data = dataloader.load_dataset(dataset)
    eval_module = MMEGeneratorDS(model_config, infer_configs)
    eval_module.eval(test_data, eval_configs)


if __name__ == '__main__':
    main()

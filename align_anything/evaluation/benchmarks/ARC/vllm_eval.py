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
import re
from typing import Any, Dict, List

from align_anything.evaluation.data_type import InferenceInput, InferenceOutput
from align_anything.evaluation.dataloader.base_dataloader import BaseDataLoader
from align_anything.evaluation.eval_logger import EvalLogger
from align_anything.evaluation.inference.vllm_inference import BaseInferencer_vllm, save_detail
from align_anything.utils.template_registry import get_eval_template_class as get_template_class
from align_anything.utils.tools import (
    custom_cfgs_to_dict,
    dict_to_namedtuple,
    load_raw_outputs,
    read_eval_cfgs,
    save_raw_outputs,
    update_dict,
)
from datasets import load_dataset


class ARCDataLoader(BaseDataLoader):
    def get_task_names(self):
        if isinstance(self.data_cfgs.task, list):
            return self.data_cfgs.task
        else:
            task_names = [self.data_cfgs.task]
            return task_names

    def get_answer(self, data):
        return data['answerKey']

    def set_fewshot_dataset(self, dataset, task):
        return dataset['validation']

    def build_example_prompt(self, data, with_answer=True, cot=False):
        choices = get_choices(data)
        answer = f'Answer: {self.get_answer(data)}' if with_answer else 'Answer: '
        return f"{data['question']}Please choose the correct answer from the following options:\n{choices}\n{answer}"

    def build_prompt(self, data):
        prompt = ''
        cot_prompt = f" Let's think step by step. "
        few_shot_examples = self.few_shot_data[: self.num_shot] if self.num_shot else []
        template = get_template_class(self.chat_template)
        if len(few_shot_examples) == 0:
            question = [
                template.system_prompt
                + template.user_prompt.format(input=prompt + self.build_example_prompt(item, False))
                + template.assistant_prompt.format(output='')
                for item in data
            ]
        else:
            if not self.cot:
                few_shots = [
                    self.build_example_prompt(
                        {key: value[i] for key, value in few_shot_examples.items()}, True
                    )
                    for i in range(len(few_shot_examples['question']))
                ]
            else:
                few_shots = [
                    f"{example['question']}\n'Answer: '{example['answer']}"
                    for example in few_shot_examples
                ]
            question = []
            for item in data:
                request = {}
                for key, value in item.items():
                    request[key] = value
                examples = few_shots + [self.build_example_prompt(request, False)]
                if self.cot:
                    question.append(
                        template.system_prompt
                        + template.user_prompt.format(input=prompt + '\n\n'.join(examples))
                        + template.assistant_prompt.format(output=cot_prompt)
                    )
                else:
                    question.append(
                        template.system_prompt
                        + template.user_prompt.format(input=prompt + '\n\n'.join(examples))
                        + template.assistant_prompt.format(output='')
                    )

        return question


class ARCGeneratorVLLM(BaseInferencer_vllm):
    def eval(
        self, data: Dict[str, List[InferenceInput]], eval_configs
    ) -> Dict[str, List[InferenceOutput]]:
        task2details = {}
        for task, input in data.items():
            task2details[task] = self.generation(input)
        return task2details


def evaluator(raw_output: List[InferenceOutput], dataloader: ARCDataLoader, task: str, file_path):
    dataset = load_dataset(dataloader.task_dir, task)[dataloader.split]
    correct_answers = []
    responses = []
    cnt_sum = 0
    cnt_match = 0
    cnt_fail = 0
    flag_fail = True
    for instance in dataset:
        correct_answers.append(
            {
                'prompt': instance['question'],
                'choices': instance['choices'],
                'answer': dataloader.get_answer(instance),
            }
        )
    for item in raw_output:
        dataloader.candidate_labels = get_candidate_labels(item.prompt)
        responses.append(
            {
                'prompt': (item.prompt),
                'answer_logprobs': get_chosen_answer(
                    item.response_logprobs[-1], dataloader.candidate_labels
                ),
                'answer': item.response[0],
            }
        )
    for correct_answer in correct_answers:
        cnt_sum += 1
        for response in responses:
            if correct_answer['prompt'] in response['prompt']:
                flag_fail = False
                chosen_answer = max(
                    response['answer_logprobs'], key=response['answer_logprobs'].get
                )
                true_or_false = judge_answer(
                    correct_answer['answer'], chosen_answer, response['answer']
                )
                if true_or_false:
                    cnt_match += 1
                save_detail(
                    correct_answer['prompt'],
                    get_choices(correct_answer),
                    correct_answer['answer'],
                    response['answer'],
                    true_or_false,
                    file_path,
                )
                break
        if flag_fail:
            cnt_fail += 1
        else:
            flag_fail = True

    return cnt_match, cnt_sum


def get_choices(data):
    if data['choices']['label'][0] == 'A':
        choices = '\n' + '\n'.join(
            [
                f"({chr(label+65)}) {data['choices']['text'][label]}"
                for label in range(len(data['choices']['text']))
            ]
        )
    else:
        choices = '\n' + '\n'.join(
            [
                f"({label}) {data['choices']['text'][label]}"
                for label in range(len(data['choices']['text']))
            ]
        )
    return choices


def get_chosen_answer(logprobs: List[Dict[str, Any]], candidate_answers: List[str]):
    answer_logprobs = {}
    for logprob in logprobs:
        key = next(iter(logprob.values())).decoded_token
        value = next(iter(logprob.values())).logprob
        if key in candidate_answers:
            answer_logprobs[key] = value
    for label in candidate_answers:
        if label not in answer_logprobs.keys():
            answer_logprobs[label] = float('-inf')
    return answer_logprobs


def get_candidate_labels(prompt):
    number_matches = re.findall(r'\([1234]\)', prompt)
    number_index = all(option in number_matches for option in ['(1)', '(2)', '(3)', '(4)'])

    if number_index:
        return ['1', '2', '3', '4']
    return ['A', 'B', 'C', 'D']


def judge_answer(correct_answer, chosen_answer, response):
    if correct_answer == chosen_answer:
        return True
    if correct_answer in ['A', 'B', 'C', 'D']:
        match = re.search(r'(?<![a-zA-Z])[A-Z](?![a-zA-Z])', response)
        if match:
            return correct_answer == match.group()
    else:
        match = re.search(r'(?<!\d)\d(?!\d)', response)
        if match:
            return correct_answer == match.group()
    return False


def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    _, unparsed_args = parser.parse_known_args()
    keys = [k[2:] for k in unparsed_args[0::2]]
    values = list(unparsed_args[1::2])
    unparsed_args = dict(zip(keys, values))

    dict_configs, infer_configs = read_eval_cfgs('arc', 'vLLM')

    try:
        assert dict_configs or infer_configs, 'Config file does not exist or is incomplete.'
    except AssertionError:
        print('Config file is not exist or incomplete.')
        exit()

    for k, v in unparsed_args.items():
        if v == '' or v is None:
            continue
        dict_configs = update_dict(dict_configs, custom_cfgs_to_dict(k, v))
        infer_configs = update_dict(infer_configs, custom_cfgs_to_dict(k, v))

    dict_configs, infer_configs = dict_to_namedtuple(dict_configs), dict_to_namedtuple(
        infer_configs
    )
    model_config = dict_configs.default.model_cfgs
    eval_configs = dict_configs.default.eval_cfgs
    logger = EvalLogger('Evaluation', log_dir=eval_configs.output_dir)
    dataloader = ARCDataLoader(dict_configs)
    assert not dataloader.cot, 'chain-of-thought cannot be used for this benchmark.'
    test_data = dataloader.load_dataset()
    eval_module = ARCGeneratorVLLM(model_config, infer_configs)
    raw_outputs_dir = os.path.join(
        eval_configs.output_dir,
        f"raw_outputs_{re.sub(r'/', '_', model_config.model_name_or_path)}.pkl",
    )
    if os.path.exists(raw_outputs_dir):
        raw_outputs = load_raw_outputs(raw_outputs_dir)
    else:
        raw_outputs = eval_module.eval(test_data, eval_configs)
        save_raw_outputs(raw_outputs, raw_outputs_dir)

    os.makedirs(logger.log_dir, exist_ok=True)
    uuid_path = f'{logger.log_dir}/{eval_configs.uuid}'
    os.makedirs(uuid_path, exist_ok=True)

    tot_num_match, tot_num_sum = 0, 0
    for task, _ in raw_outputs.items():
        file_path = f'{uuid_path}/{task}.json'
        cnt_match, cnt_sum = evaluator(raw_outputs[task], dataloader, task, file_path)
        tot_num_match += cnt_match
        tot_num_sum += cnt_sum

        eval_results = {
            'model_id': [dict_configs.default.model_cfgs.model_id],
            'num_fewshot': [eval_configs.n_shot],
            'chain_of_thought': [eval_configs.cot],
            'num_match': [cnt_match],
            'num_sum': [cnt_sum],
            'accuracy': [cnt_match / cnt_sum],
        }
        logger.print_table(title=f'ARC/{task} Benchmark', data=eval_results)
        logger.log('info', '+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++')
        logger.log('info', f'task: {task}')
        logger.log('info', f"model_id: {eval_results['model_id'][0]},")
        logger.log('info', f"num_fewshot: {eval_results['num_fewshot'][0]},")
        logger.log('info', f"chain_of_thought: {eval_results['chain_of_thought'][0]},")
        logger.log('info', f"num_match: {eval_results['num_match'][0]},")
        logger.log('info', f"num_sum: {eval_results['num_sum'][0]},")
        logger.log('info', f"accuracy: {eval_results['accuracy'][0]},")
        logger.log('info', '+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++')

    eval_results = {
        'model_id': [dict_configs.default.model_cfgs.model_id],
        'num_fewshot': [eval_configs.n_shot],
        'chain_of_thought': [eval_configs.cot],
        'tot_num_match': [tot_num_match],
        'tot_num_sum': [tot_num_sum],
        'tot_accuracy': [tot_num_match / tot_num_sum],
    }
    logger.print_table(title=f'ARC Benchmark', data=eval_results)
    logger.log('info', '+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++')
    logger.log('info', f"model_id: {eval_results['model_id'][0]},")
    logger.log('info', f"num_fewshot: {eval_results['num_fewshot'][0]},")
    logger.log('info', f"chain_of_thought: {eval_results['chain_of_thought'][0]},")
    logger.log('info', f"tot_num_match: {eval_results['tot_num_match'][0]},")
    logger.log('info', f"tot_num_sum: {eval_results['tot_num_sum'][0]},")
    logger.log('info', f"tot_accuracy: {eval_results['tot_accuracy'][0]},")
    logger.log('info', '+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++')


if __name__ == '__main__':
    main()

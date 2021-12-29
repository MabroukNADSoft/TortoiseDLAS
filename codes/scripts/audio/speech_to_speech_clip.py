import argparse
import functools
import os
from multiprocessing.pool import ThreadPool

import torch
import torch.nn as nn
import torch.nn.functional as F
import yaml
from tqdm import tqdm

from data.audio.unsupervised_audio_dataset import load_audio
from data.util import is_wav_file, find_files_of_type, is_audio_file
from models.audio_resnet import resnet34, resnet50
from models.tacotron2.taco_utils import load_wav_to_torch
from scripts.audio.gen.speech_synthesis_utils import wav_to_mel
from scripts.byol.byol_extract_wrapped_model import extract_byol_model_from_state_dict
from utils.options import Loader
from utils.util import load_model_from_config

clip_model = None


def recursively_find_audio_directories(root):
    subdirs = []
    audio_files = []
    for f in os.scandir(root):
        if f.is_dir():
            subdirs.append(f)
        elif is_audio_file(f.path):
            audio_files.append(f.path)
    assert len(subdirs) == 0 or len(audio_files) == 0
    if len(subdirs) > 0:
        res = []
        for subdir in subdirs:
            res.extend(recursively_find_audio_directories(subdir.path))
        return res
    return [(root, audio_files)]


def process_subdir(subdir, options, clip_sz):
    global clip_model
    if clip_model is None:
        print('Loading CLIP model..')
        clip_model = load_model_from_config(preloaded_options=options, model_name='clip', also_load_savepoint=True)

    root, paths = subdir
    root = str(root)
    output_file = os.path.join(root, 'similarities.pth')
    if os.path.exists(output_file):
        print(f'{root} already processed. Skipping.')
        return
    print(f'Processing {root}..')

    clips = []
    for path in paths:
        clip = load_audio(str(path), 22050)
        padding = clip_sz - clip.shape[1]
        if padding > 0:
            clip = F.pad(clip, (0, padding))
        elif padding < 0:
            clip = clip[:, :clip_sz]
        clips.append(clip)
    sims = None
    while len(clips) > 0:
        stacked = torch.stack(clips[:256], dim=0).cuda()
        clips = clips[256:]
        mels = wav_to_mel(stacked)
        outp = clip_model.inference(mels)
        if sims is None:
            sims = outp
        else:
            if outp.shape[-1] != 256:
                outp = F.pad(outp, (0,256-outp.shape[-1]))
            sims = torch.cat([sims, outp], dim=0)

    simmap = {}
    # TODO: this can be further improved. We're just taking the topk here but, there is no gaurantee that there is 3
    # samples from the same speaker in any given folder.
    for path, sim in zip(paths, sims):
        n = min(4, len(sim))
        top3 = torch.topk(sim, n)
        rel = os.path.relpath(str(path), root)
        simpaths = []
        if n == 1:
            simpaths.append(rel)
        else:
            for i in range(1,n):  # The first entry is always the file itself.
                top_ind = top3.indices[i]
                simpaths.append(os.path.relpath(paths[top_ind], root))
        simmap[rel] = simpaths
    torch.save(simmap, output_file)


if __name__ == '__main__':
    """
    This script iterates within a directory filled with subdirs. Each subdir contains a list of audio files from the same
    source. The script uses an speech-to-speech clip model to find the <n> most similar audio clips within each subdir for
    each clip within that subdir.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-o', type=str, help='Path to the options YAML file used to train the CLIP model', default='../options/train_voice_voice_clip.yml')
    parser.add_argument('--num_workers', type=int, help='Number concurrent processes to use', default=1)
    parser.add_argument('--root_path', type=str, help='Root path to search for audio directories from', default='Y:\\clips\\podcasts-0\\5177_20190625-Food Waste is Solvable')
    parser.add_argument('--clip_size', type=int, help='Amount of audio samples to pull from each file', default=22050)
    args = parser.parse_args()

    with open(args.o, mode='r') as f:
        opt = yaml.load(f, Loader=Loader)

    print("Finding applicable files..")
    all_files = recursively_find_audio_directories(args.root_path)
    print(f"Found {len(all_files)}. Processing.")
    fn = functools.partial(process_subdir, options=opt, clip_sz=args.clip_size)
    if args.num_workers > 1:
        with ThreadPool(args.num_workers) as pool:
            tqdm(list(pool.imap(fn, all_files)), total=len(all_files))
    else:
        for subdir in tqdm(all_files):
            fn(subdir)



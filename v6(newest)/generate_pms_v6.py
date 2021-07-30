# coding=utf-8
# Copyright (c) 2019, NVIDIA CORPORATION.  All rights reserved.
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

"""Sample Generate GPT2"""

import os
import random
import numpy as np
import torch
import torch.nn.functional as F
import argparse
import time
from datetime import datetime
from arguments import get_args
from utils import Timers
from pretrain_gpt2 import initialize_distributed
from pretrain_gpt2 import set_random_seed
from pretrain_gpt2 import get_train_val_test_data
from pretrain_gpt2 import get_masks_and_position_ids
from utils import load_checkpoint, get_checkpoint_iteration
from data_utils import make_tokenizer
from configure_data import configure_data
import mpu
import deepspeed
import copy
from fp16 import FP16_Module
from model import GPT2Model
from model import DistributedDataParallel as DDP
from utils import print_rank_0
from pretrain_gpt2 import get_model
from pypinyin import pinyin,FINALS, FINALS_TONE,TONE3
import jsonlines
from new_tkl import cilin
from new_tkl import checkrhyself
from new_tkl import checkrhy
from new_tkl import checksentence
from new_tkl import getrhy
def setup_model(args):
    """Setup model and optimizer."""

    model = get_model(args)

    # if args.deepspeed:
    #     print_rank_0("DeepSpeed is enabled.")
    #
    #     model, _, _, _ = deepspeed.initialize(
    #         model=model,
    #         model_parameters=model.parameters(),
    #         args=args,
    #         mpu=mpu,
    #         dist_init_required=False
    #     )
    if args.load is not None:
        if args.deepspeed:
            iteration, release, success = get_checkpoint_iteration(args)
            print(iteration)
            path = os.path.join(args.load, str(iteration), "mp_rank_00_model_states.pt")
            checkpoint = torch.load(path)
            model.load_state_dict(checkpoint["module"])
        else:
            _ = load_checkpoint(
                model, None, None, args, load_optimizer_states=False)
    # if args.deepspeed:
    #     model = model.module

    return model


def get_batch(context_tokens, device, args):
    tokens = context_tokens
    tokens = tokens.view(args.batch_size, -1).contiguous()
    tokens = tokens.to(device)

    # Get the masks and postition ids.
    attention_mask, loss_mask, position_ids = get_masks_and_position_ids(
        tokens,
        args.eod_token,
        reset_position_ids=False,
        reset_attention_mask=False,
        transformer_xl=args.transformer_xl,
        mem_length=args.mem_length)

    return tokens, attention_mask, position_ids

 
    
def generate_score(model, tokenizer, args, device,  mid_str, eval_str, raw_mems=None):
    penalty=0
    title=mid_str.split("此句出自")[0]
    for i in eval_str:
        if i in title:
            penalty+=1
    context_count = 0
    model.eval()
    
   
    mems = []
   

    def build_mask_matrix(query_length, key_length, sep=0, device='cuda'):
        m = torch.ones((1, query_length, key_length), device=device)
        assert query_length <= key_length
        m[0, :, -query_length:] = torch.tril(m[0, :, -query_length:])
        m[0, :, :sep + (key_length - query_length)] = 1
        m = m.unsqueeze(1)
        return m
    #penalty on same word

    model.eval()
    with torch.no_grad():
               
        mid_tokens = tokenizer.EncodeAsIds(mid_str).tokenization
        eval_tokens = tokenizer.EncodeAsIds(eval_str).tokenization
        context_tokens=mid_tokens

        context_length = len(context_tokens)
        eval_length = len(eval_tokens)

        context_tokens_tensor = torch.cuda.LongTensor(context_tokens  + eval_tokens)

        tokens, attention_mask, position_ids = get_batch(context_tokens_tensor, device, args)
        # print(context_tokens)
        start_time = time.time()

        index = 0
        logits, *mems = model(tokens[:, index: ],
            torch.arange(index, tokens.shape[1], dtype=torch.long, device=tokens.device).unsqueeze(0),
            build_mask_matrix(tokens.shape[1] - index, tokens.shape[1], device=tokens.device),
            *mems)
            
        logits = logits[:, -eval_length-1:-1]
        log_probs = F.softmax(logits, dim=-1)
        log_num = torch.log(log_probs).data.clamp(min=-35, max=100000)

        log_nums = [
            log_num[0, i, eval_token]
            for i, eval_token in enumerate(eval_tokens) # TODO eos
        ]
        #print(log_nums)

        sumlognum = sum(log_nums)
           
        del logits
        del mems
        torch.cuda.empty_cache()
        
    return sumlognum-2.5*(penalty**2.5)
    
    
def generate_sentence(model,tokenizer,args,device,current_tokens,mems,dic,wdic,endnote=[",","，","?","？"],num_candidates=10,min_length=5,max_length=7,yayun=None,rhy=0):
    model.eval()
    #yayun=None
    #rhy: 0-- free mode, free end  1-- |-|  2-- -|-  +3-- |end +6-- -end
    
    with torch.no_grad():
        #index=len(tokens[0])
        mct_tree=[]
        if min_length!=max_length:
            mems=[]
            tokens, attention_mask, position_ids = get_batch(current_tokens, device, args)
            logits,*rts = model(tokens, position_ids, attention_mask, *mems)
        else:
            tokens=current_tokens
            index=len(tokens[0])
            logits,*rts=model(tokens[:, index - 1: index], tokens.new_ones((1, 1)) * (index - 1),
                        tokens.new_ones(1, 1, 1, args.mem_length + 1, device=tokens.device,
                                                            dtype=torch.float), *mems)
                                                            
        output_tokens_list = tokens.view(-1).contiguous()
        original_context=tokenizer.DecodeIds(output_tokens_list.tolist())
        context_length=len(tokens[0])
        logits=logits[0,-1]
        #mct_structure=-np.ones(len(logits))
        mct_tree.append([logits,rts,tokens,-np.ones(len(logits)),torch.ones(len(logits)).cuda(),0])
        #print(logits.shape)
        final_result=[]
        nextid=0
        tries=0
        max_tries=num_candidates*30
        curvote=1
        if ',' in endnote:
            curvote=0
        if ',' in endnote:
            endid=43359
        else:
            endid=43361
        dpcount=0
    
        tmp=args.temperature
        while (len(final_result)<num_candidates)and(tries<max_tries) and (tries<1000):
            currentid=nextid
            tries+=1
            while currentid!=-1:
                tc=torch.log(mct_tree[currentid][4])
                tc=tc+F.relu(tc-10)*1000
                logits=mct_tree[currentid][0].view(-1)-tc*0.5
                logits=logits[:50001]
                log_probs = F.softmax(logits, dim=-1)
              
                pr=torch.multinomial(log_probs,num_samples=1)[0]
                #pr=torch.argmax(logits)
                prev=pr.item()
                #print(logits.shape,currentid,prev)
                mct_tree[currentid][4][prev]+=1
                lastid=currentid
                currentid=int(mct_tree[currentid][3][prev])
            #start from lastid & currentid
            
            cqs=mct_tree[lastid][2]
            #print(pr)
            tokens = torch.cat((cqs, pr.unsqueeze(0).view(1, 1)), dim=1)
            output_tokens_list = tokens.view(-1).contiguous()
            #if max_length==min_length:
             #   print(min_length,output_tokens_list,context_length)
            #print(output_tokens_list[context_length:])
            sentence = tokenizer.DecodeIds(output_tokens_list[context_length:].tolist())
            
            #print(output_tokens_list[context_length:],context_length,sentence)
            logit=mct_tree[lastid][0]
            log_probs = F.softmax(logit, dim=-1)
            log_pbs=torch.log(log_probs)
            score=log_pbs[prev].item()
            nextid=0
            yaw=yayun
            if curvote==0:
                yaw=None
            ip=checksentence(sentence,original_context,min_length,max_length,endnote,dic,wdic,curvote=curvote,yayun=yaw,rhy=rhy)
            
            for j in final_result:
                if j[0]==sentence:
                    ip=1
                if ('<|end' in sentence) and ('<|end' in j[0]):
                    ip=1
                    
            score=mct_tree[lastid][5]+score
            if (ip==1):
                nextid=lastid
                dpcount+=1
                max_tries+=1
                if (dpcount>=50) or (dpcount>=8 and len(sentence)<max_length):
                    nextid=0
                    dpcount=0
                mct_tree[lastid][4][prev]=100000
                continue
            dpcount=0
            if (ip==0):
                mct_tree[lastid][4][prev]=100000
                yay=yayun
                rh=0
                
                if not("end" in sentence):
                    if not(sentence[-2] in wdic):
                        continue
                    ss=wdic[sentence[-2]]
                    
                    if (curvote==1) or (curvote==0 and len(ss)==1 and ss[0]==1):
                        if yayun==None:
                            yay=sentence[-2]
                        else:
                            yay=yayun+sentence[-2]
                    rh=getrhy(sentence,rhy,dic)
                final_result.append([copy.deepcopy(sentence),copy.deepcopy(score),copy.deepcopy(tokens),copy.deepcopy(mct_tree[lastid][1]),yay,rh])
                #print(sentence,score)
                continue
        
           
            
                #calculate
            mct_tree[lastid][3][prev]=len(mct_tree)
            tmp=args.temperature
            if (len(sentence)>=4 or (len(sentence)==3 and max_length==5)):
                tmp=tmp*0.8
            rts=mct_tree[lastid][1]
            index=len(tokens[0])
            
            
            logits,*rts=model(tokens[:, index - 1: index], tokens.new_ones((1, 1)) * (index - 1),
                        tokens.new_ones(1, 1, 1, args.mem_length + 1, device=tokens.device,
                                                                dtype=torch.float), *rts)
            logits=logits[0,-1]/tmp
            if len(sentence)==max_length:
                logits[endid]+=10
            for i in output_tokens_list[context_length:].tolist():
                logits[i]-=2
                
            mct_tree.append([logits,rts,tokens,-np.ones(len(logits)),torch.ones(len(logits)).cuda(),score])
            nextid=len(mct_tree)-1
            
                
        del mct_tree
        torch.cuda.empty_cache()
        #print(tries,len(final_result))
        return final_result
        
        

        
def getlength(str):
    w=str.replace('。',',').replace('，',',').replace('？',',').replace('?',',').replace(' ',',').replace('！',',').replace('!',',').replace(':',',').replace(' ','')
    sp=w.split(',')
    
    return len(sp[-2])

def getlastsentence(str):
    w=str.replace('。',',').replace('，',',').replace('？',',').replace('?',',').replace(' ',',').replace('！',',').replace('!',',').replace(':',',').replace(' ','')
    sp=w.split(',')
    fom=sp[-1]
    if len(fom)==0:
        fom=sp[-2]
    return fom+str[-1]

def get2sentencebefore(str):
    w=str.replace('。',',').replace('，',',').replace('？',',').replace('?',',').replace(' ',',').replace('！',',').replace('!',',').replace(':',',').replace(' ','')
    sp=w.split(',')
    idk=-1
    while len(sp[idk])==0:
        idk-=1
    idk-=1
    while len(sp[idk])==0:
        idk-=1
    return sp[idk]

def check2compare(sentence1,sentence2,imp,wdic):
    s1=sentence1.replace('。','').replace('，','').replace('？','').replace('?','').replace('  ','').replace('！','').replace('!','').replace(',','')
    s2=sentence2.replace('。','').replace('，','').replace('？','').replace('?','').replace(' ','').replace('！','').replace('!','').replace(',','')
    if len(s1)!=len(s2):
        return -1000
    num=0
    for i in range(len(s1)):
        if s1[i]==s2[i]:
           num+=1
        
    score=0.5-num*num*2.5
           
    
    w1=wdic[s1[-1]]
    w2=wdic[s2[-1]]
    score-=imp*0.75
    if (s1[-1]==s2[-1]):
        score-=imp*3.5
    for i in w1:
        if i in w2:
            score+=imp*1.5
            break
    
    
        
    return score

def check2com(sentence,org_context,imp,dic,wdic):
    
    before2=get2sentencebefore(org_context)
    before1=getlastsentence(org_context)[:-1]
    s1=check2compare(sentence,before2,imp,wdic)
    if imp==1:
        s2=checkrhy(sentence,before1,imp+0.5,dic,req=1)
    else:
        s2=checkrhy(sentence,before1,imp,dic)
    sc=s1+s2
    
    org=org_context.replace('。',',').replace('!',',').replace('?',',')
    for i in range(len(sentence)-1):
        if sentence[i] in org:
            sc-=3
            if sentence[i:i+2] in org:
                sc-=5
                if (i==len(sentence)-2):
                    sc-=35
            
        
    return sc
    
    
    
    
    
def generate_string(model, tokenizer, args, device,title,author,desc=None,length=None):
    input_str=title+" 作者:"+author+" 体裁:诗歌 题名:"+title+" 正文: "
    if desc is not None:
        input_str=title+" 作者:"+author+" 体裁:诗歌 描述:"+desc+" 题名:"+title+" 正文: "
    #aus=author.split(' ')[1]
    wdic,dic,a1,a2=cilin()
    #print(wdic,dic)
    input_len=len(input_str)
    context_count=0
    model.eval()
    with torch.no_grad():
        context_tokens = tokenizer.EncodeAsIds(input_str).tokenization
        eo_tokens=tokenizer.EncodeAsIds('<|endoftext|>').tokenization
        context_length = len(context_tokens)
        if context_length>=args.seq_length:
            return 0,"输入过长。"
      

        context_tokens_tensor = torch.cuda.LongTensor(context_tokens)
        eo_token_tensor=torch.cuda.LongTensor(eo_tokens)
        context_length_tensor = torch.cuda.LongTensor([context_length])
        context_length = context_length_tensor[0].item()
        #tokens, attention_mask, position_ids = get_batch(context_tokens_tensor, device, args)

        start_time = time.time()

        counter, mems = 0, []
        org_context_length = context_length
        beam_size=10
        beam_candidate=7
        beam_max=2
        max_headings=4
        final_storage=[]
        final_storage_score=[]
        step=9
        overall_score=[]
        past_beam_id=[]
        #print(counter,beam_tokens,beam_score)
        if length is None:
            beam_sentences=generate_sentence(model,tokenizer,args,device,context_tokens_tensor,[],dic,wdic,num_candidates=beam_size*5)
        if length==5:
            beam_sentences=generate_sentence(model,tokenizer,args,device,context_tokens_tensor,[],dic,wdic,num_candidates=beam_size*5,max_length=6)
        if length==7:
            beam_sentences=generate_sentence(model,tokenizer,args,device,context_tokens_tensor,[],dic,wdic,num_candidates=beam_size*5,min_length=6)
        for w in range(len(beam_sentences)):
            if '<|end' in beam_sentences[w][0]:
                continue
            input='”'+beam_sentences[w][0]+'”此句出自'
            output_str='古诗《'+title+'》'
            score1=generate_score(model,tokenizer,args,device,input,output_str)
            '''
            input='”'+beam_sentences[w][0]+'”此句作者为'
            output_str=aus
            score2=generate_score(model,tokenizer,args,device,input,output_str)
            '''
            ss=-beam_sentences[w][1]/len(beam_sentences[w][0])-8
            iscore=score1-0.45*(np.abs(ss)+ss)
            beam_sentences[w][1]=iscore
            #print(beam_sentences[w][0],beam_sentences[w][1])
            overall_score.append(iscore.cpu())
            past_beam_id.append(w)
            
        gy=np.argsort(overall_score)
        k=0
        sumbeam=np.zeros(100)
        
        gym=[]
        num=0
        while (num<beam_size)and (k<=len(gy)):
           k+=1
           if sumbeam[past_beam_id[gy[-k]]]<beam_max:
            sumbeam[past_beam_id[gy[-k]]]+=1
            gym.append(gy[-k])
            num+=1
        best_score=-1000
        best_pos=0
        for i in range(step):
            if (best_score>-1000) and (i>8):
                del beam_sentences
                del beam_new_sentences
                torch.cuda.empty_cache()
                return final_storage,final_storage_score
            beam_new_sentences=[]
            
            endnote=[',','，','?','？']
            if i%2==0:
                endnote=['。','?','？','！','!']
            overall_score=[]
            past_beam_id=[]
            size=beam_size
            if len(gym)<size:
                size=len(gym)
            if size==0:
                del beam_sentences
                del beam_new_sentences
                torch.cuda.empty_cache()
                return final_storage,final_storage_score
            ini_score=beam_sentences[gym[0]][1]/(i+1)
            # early stopping
            if i>7:
                ini_score-=0.2
            if i>11:
                ini_score-=0.4
            
            if ini_score<best_score-2:
                del beam_sentences
                del beam_new_sentences
                torch.cuda.empty_cache()
                
                return final_storage,final_storage_score
            
            # parallel starts
            for w in range(size):
                id=gym[w]
                current_sentence=input_str+beam_sentences[id][0]
                
               #print(beam_sentences[id][0],beam_sentences[id][1])
                ini_score=beam_sentences[id][1]
                token_tensor=beam_sentences[id][2]
                mems=beam_sentences[id][3]
            
                len_sentence=getlength(beam_sentences[id][0])
                '''
                if i>=15:
                    final_storage.append(copy.deepcopy(current_sentence[input_len:]))
                    sc=beam_sentences[id][1]/(i+1)
                    sc-=2
                    final_storage_score.append(sc)
                    print(current_sentence,final_storage_score[-1])
                    continue
                '''
                #print(token_tensor)
                old_rhy=beam_sentences[id][-1]
                if i%2==0:
                    new_rhy=3+(3-old_rhy)
                    if old_rhy==0:
                        new_rhy-=3
                else:
                    new_rhy=6+old_rhy
                
                gen=generate_sentence(model,tokenizer,args,device,token_tensor,mems,dic,wdic,num_candidates=beam_candidate,endnote=endnote,min_length=len_sentence,max_length=len_sentence,yayun=beam_sentences[id][-2],rhy=new_rhy)
                for jj in gen:
                    if '<|end' in jj[0]:
                        if (i%2==1 and i>=3):
                            final_storage.append(copy.deepcopy(current_sentence[input_len:]))
                            sc=beam_sentences[id][1]/(i+1) #prioritize short poems
                            sc=sc.item()
                            if (i==5 or i==9 or i==13):
                                sc-=1.5
                            if (i==15):
                                sc-=0.6
                            if (i==11):
                                sc-=0.4
                            if (i==3):
                                sc+=0.2
                            if sc>best_score:
                                best_score=sc
                                best_pos=len(final_storage)-1
                            sc=np.abs(sc)
                            final_storage_score.append(sc)
                            print(current_sentence,final_storage_score[-1])
                        
                        continue
                    st=jj[0]
                    # experiment shows that this is better universal,
                    if (i%2==0):
                        st=getlastsentence(beam_sentences[id][0])+jj[0]
                    else:
                        st=get2sentencebefore(beam_sentences[id][0])+','+getlastsentence(beam_sentences[id][0])+jj[0]
                    input='”'+st+'”此句出自'
                    
                    output_str='古诗《'+title+'》'
                    
                    score1=generate_score(model,tokenizer,args,device,input,output_str)
                    '''
                    input='”'+st+'”此句作者为'
                    output_str=aus
                    score2=generate_score(model,tokenizer,args,device,input,output_str)
                    '''
                    factor=1
                    
                    ss=-jj[1]/len(jj[0])-8
                    iscore=score1-0.45*(np.abs(ss)+ss)
                    if i>=1:
                        imp=1
                        if i%2==0:
                            imp+=1.5
                        scorem=check2com(jj[0],beam_sentences[id][0],imp,dic,wdic)
                        
                        iscore+=scorem
                        
                    
                    jj[0]=beam_sentences[id][0]+jj[0]
                    jj[1]=iscore+ini_score
                    #print(i,beam_sentences[id][0],jj[1])
                    #print(i,jj[0],jj[1]/(i+2))
                    beam_new_sentences.append(jj)
                    overall_score.append(jj[1].cpu())
                    past_beam_id.append(w)
            del beam_sentences
            torch.cuda.empty_cache()
            beam_sentences=beam_new_sentences
            gy=np.argsort(overall_score)
            sumbeam=np.zeros(100)
            sumheading={}
            k=0
            gym=[]
            num=0
            while (num<beam_size) and (k+1<len(past_beam_id)):
                k+=1
                
                if sumbeam[past_beam_id[gy[-k]]]<beam_max:
                    wd=beam_sentences[gy[-k]][0][:5]
                    
                    if (not(wd in sumheading)) or (sumheading[wd]<max_headings):
                        if not(wd in sumheading):
                            sumheading[wd]=1
                        else:
                            sumheading[wd]+=1
                        sumbeam[past_beam_id[gy[-k]]]+=1
                        gym.append(gy[-k])
                        num+=1
                        #print(i,beam_sentences[gy[-k]][0],beam_sentences[gy[-k]][1]/(i+2))
                        
             #parallel ends
            
        
        del beam_sentences
        del beam_new_sentences
        torch.cuda.empty_cache()
        
        return final_storage,final_storage_score
        
            

def prepare_tokenizer(args):
    tokenizer_args = {
        'tokenizer_type': args.tokenizer_type,
        'corpus': None,
        'model_path': args.tokenizer_path,
        'vocab_size': args.vocab_size,
        'model_type': args.tokenizer_model_type,
        'cache_dir': args.cache_dir}
    tokenizer = make_tokenizer(**tokenizer_args)

    num_tokens = tokenizer.num_tokens
    before = num_tokens
    after = before
    multiple = args.make_vocab_size_divisible_by * \
               mpu.get_model_parallel_world_size()
    while (after % multiple) != 0:
        after += 1
    print_rank_0('> padded vocab (size: {}) with {} dummy '
                 'tokens (new size: {})'.format(
        before, after - before, after))

    args.tokenizer_num_tokens = after
    args.tokenizer_num_type_tokens = tokenizer.num_type_tokens
    args.eod_token = tokenizer.get_command('eos').Id

    # after = tokenizer.num_tokens
    # while after % mpu.get_model_parallel_world_size() != 0:
    #     after += 1

    args.vocab_size = after
    print("prepare tokenizer done", flush=True)

    return tokenizer

def set_args():
    args=get_args()
    print(args.gpu)
    os.environ["CUDA_VISIBLE_DEVICES"]=args.gpu
    #set up
    #print(args)
    args.deepspeed=True
    args.num_nodes=1
    args.num_gpus=1
    args.model_parallel_size=1
    args.deepspeed_config="script_dir/ds_config.json"
    args.num_layers=32
    args.hidden_size=2560
    #args.load="/workspace/xuzou/useful_models/ts_human"
    args.load="/workspace/xuzou/useful_models/poems/dl"
    args.num_attention_heads=32
    args.max_position_embeddings=1024
    args.tokenizer_type="ChineseSPTokenizer"
    args.cache_dir="cache"
    args.fp16=True
    args.out_seq_length=180
    args.seq_length=200
    args.mem_length=256
    args.transformer_xl=True
    args.temperature=1
    args.top_k=0
    args.top_p=0
    
    return args
def prepare_model():
    """Main training program."""

    #print('Generate Samples')

    # Disable CuDNN.
    torch.backends.cudnn.enabled = False

    # Timer.
    timers = Timers()

    # Arguments.
    args = set_args()
    #print(args)
    args.mem_length = args.seq_length + args.mem_length - 1
    

    # Pytorch distributed.
    initialize_distributed(args)

    # Random seeds for reproducability.
    args.seed=random.randint(0,1000000)
    set_random_seed(args.seed)

    #get the tokenizer
    tokenizer = prepare_tokenizer(args)

    # Model, optimizer, and learning rate.
    model = setup_model(args)
    #args.load="../ckp/txl-2.8b11-20-15-10"
    #model2=setup_model(args)
    #setting default batch size to 1
    args.batch_size = 1

    #generate samples
    return model,tokenizer,args

def generate_strs(tups):
    model,tokenizer,args=prepare_model()
    output=[]
    for tup in tups:
        #str=generate_token_tensor(str,tokenizer)
        
        output_string,output_scores=generate_string(model,tokenizer, args, torch.cuda.current_device(),tup[0],tup[1],desc=tup[2])
        list_poems=0
        
        ranklist=np.argsort(output_scores)
        best_score=output_scores[ranklist[0]]
        text_dir="poems_save/"
        already=[]
        with jsonlines.open(text_dir+tup[0]+tup[1]+'.jsonl', mode='w') as writer:
            for i in range(len(ranklist)):
                j=ranklist[i]
                if output_scores[j]<best_score+2:
                    if not(output_string[j][0:15] in already):
                        otc={}
                        otc['author']=tup[1]
                        otc['title']=tup[0]
                        otc['context']=output_string[j]
                        #print(otc)
                        writer.write(otc)
                        already.append(output_string[j][0:15])
                        
        
        
    return 0

    

def generate():
    fi=[]
    
    title_list=[["咏特朗普","咏美国前总统，民粹主义政客唐纳德 特朗普"]]
    title_list=[["人工智能专家组成立大会","新一代人工智能管理专家组成立大会，31位技术过硬、有奉献精神、有责任担当的知名专家为人工智能项目实施保驾护航，希望通过专家组的工作经历为国家储备战略科学家，为2030重大项目的组织实施提供样板。"]]
    author_list=["李白","杜甫","华智冰","王勃"]
    
    for j in author_list:
        for i in title_list:
            fi.append([i[0],j,i[1]])
           
            #fi.append([i[0],j,i[1]])
    output=generate_strs(fi)
    return 0
    

def random_generate(mode=0):
    text_dir="poems_save_new2/"
            
    #model,tokenizer,args=prepare_model()
    if mode==1:
        text_dir="poems_save_new6/"

    if mode==2:
        text_dir="poems_save_xinhua_news3/"
    if mode==3:
        text_dir="poems_save_poemnets/"
        mode=1
        
    dist=os.listdir()
    if not(text_dir[:-1] in dist):
        os.mkdir(text_dir[:-1])
    if mode==0:
        qts=open("selected_modern.txt",'r')
        
        wt=qts.readlines()
        lt=[]
        for i in wt:
            
            if len(i)>2:
                sp=i.split()
                #print(sp)
                author="李白"
                title=sp[0]
                num_wd=int(sp[2])
                num_st=int(sp[3])
                lt.append([author,title,num_wd,num_st])
        qts.close()
    if mode==1:
        qts=open("index.txt",'r')
        
        wt=qts.readlines()
        lt=[]
        for i in wt:
            
            sp=i.split()
            if len(sp)>0:
            #print(sp)
                author="李白"
                title=sp[0]
                lt.append([author,title])
        qts.close()
    if mode==2:
        qts=open("mars.txt",'r')
        qts2=open("carbon.txt",'r')
        wt=qts.readlines()
        wt2=qts2.readlines()
        
        lt=[wt,wt2]
        import json
        for j in lt:
            for jj in j:
            
                if '{' in jj:
                    #print(jj)
                    sp=json.loads(jj)
                    
                    if len(sp)>0:
                    #print(sp)
                        author="李白"
                        title=sp['headLine']
                        desc=sp['content'].replace('\u3000','')
                        desc=desc.split('\n')[0]
                        lt.append([author,title,desc])
        qts.close()
        qts2.close()
        
    model,tokenizer,args=prepare_model()
    while True:
        id=random.randint(0,len(lt)-1)
        if mode==0:
            author,title,num_wd,num_st=lt[id]
        if mode==1:
            author,title=lt[id]
        if mode==2:
            author,title,desc=lt[id]
        lists=os.listdir(text_dir)
        lts=title+author+'.jsonl'
        if (lts in lists):
            continue
        #str=generate_token_tensor(str,tokenizer)
        if mode==0:
            output_string,output_scores=generate_string(model, tokenizer, args, torch.cuda.current_device(),title,author,length=num_wd)
        if mode==1:
            output_string,output_scores=generate_string(model, tokenizer, args, torch.cuda.current_device(),title,author)
        if mode==2:
            output_string,output_scores=generate_string(model, tokenizer, args, torch.cuda.current_device(),title,author,desc=desc)
        new_output_string=[]
        new_output_score=[]
        for i in range(len(output_string)):
            st=output_string[i].replace('。',',').replace('，',',').replace('？',',').replace('?',',').replace('!',',').replace('！',',')
            st=st.split(',')
                #print(st,num_st)
            if mode==0:
                if len(st)-1==num_st:
                    new_output_string.append(output_string[i])
                    new_output_score.append(output_scores[i])
            else:
                new_output_string.append(output_string[i])
                new_output_score.append(output_scores[i])
                
        if len(new_output_string)==0:
            del output_string
            del output_scores
            continue
        
        list_poems=0
        
        ranklist=np.argsort(new_output_score)
        best_score=new_output_score[ranklist[0]]
        
        already=[]
        
        with jsonlines.open(text_dir+title+author+'.jsonl', mode='w') as writer:
            for i in range(len(ranklist)):
                j=ranklist[i]
                if new_output_score[j]<best_score+2:
                    if not(new_output_string[j][0:5] in already):
                    
                        otc={}
                        otc['author']=author
                        otc['title']=title
                        otc['context']=new_output_string[j]
                        #print(otc)
                        writer.write(otc)
                        already.append(new_output_string[j][0:5])
        
        del output_string
        del output_scores
        
    return 0

def poem_interface(model,tokenizer, args, device,title,author,desc=None):
    if not(desc is None):
        output_string,output_scores=generate_string(model,tokenizer, args, device,title,author,desc=desc)
    else:
        output_string,output_scores=generate_string(model,tokenizer, args, device,title,author)
        
    list_poems=[]
        
    ranklist=np.argsort(output_scores)
    best_score=output_scores[ranklist[0]]
    for i in range(len(ranklist)):
        j=ranklist[i]
        if output_scores[j]<best_score+2:
            list_poems.append(output_string[j])
    
    return list_poems
    

random_generate(mode=1)


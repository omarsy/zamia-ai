#!/usr/bin/env python
# -*- coding: utf-8 -*- 

#
# Copyright 2015, 2016, 2017 Guenter Bartsch
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
# 
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#
# Zamia AI MQTT server
#
# can be used for chatbot-like applications (text-only)
# as well as full-blown speech i/o based home assistant type
# setups
#

# Text NLP Processing
# -------------------
# 
# * topic `ai/input/text`
# * payload (JSON encoded dict): 
#   * "utt"  : utterance to be processed 
#   * "lang" : language of utterance
#   * "user" : user who uttered the utterance
# 
# publishes:
#
# * topic `ai/response`
# * payload:
#   * "utt"     : utterance
#   * "intents" : intents
# 
# Example:
#
# hbmqtt_pub --url mqtt://dagobert -t ai/input/text -m '{"utt":"hello computer","lang":"en","user":"tux"}'

import os
import sys
import logging
import traceback
import json
import random
import time
import datetime
import dateutil
import wave
import struct

import paho.mqtt.client as mqtt
import numpy            as np

from optparse             import OptionParser
from threading            import RLock, Lock, Condition

from zamiaai              import model
from zamiaprolog.builtins import ASSERT_OVERLAY_VAR_NAME
from zamiaai.ai_kernal    import AIKernal
from aiprolog.runtime     import USER_PREFIX
from zamiaprolog.logicdb  import LogicDB
from nltools              import misc
from nltools.tts          import TTS
from nltools.asr          import ASR, ASR_ENGINE_NNET3

PROC_TITLE        = 'ai_mqtt'
AI_SERVER_MODULE  = '__server__'

AI_USER           = 'server' # FIXME: some sort of presence information maybe?
SAMPLE_RATE       = 16000
MAX_AUDIO_AGE     = 2        # seconds, ignore any audio input older than this
ATTENTION_SPAN    = 30       # seconds
AUDIO_LOC_CNT     = 5        # seconds
AUDIO_EXTRA_DELAY = 0.5      # seconds

TOPIC_INPUT_TEXT  = 'ai/input/text'
TOPIC_INPUT_AUDIO = 'ai/input/audio'
TOPIC_CONFIG      = 'ai/config'
TOPIC_RESPONSE    = 'ai/response'
TOPIC_INTENT      = 'ai/intent'
TOPIC_STATE       = 'ai/state'

DEFAULTS = {
            'broker_host'   : 'localhost',
            'broker_port'   : '1883',
            'broker_user'   : '',
            'broker_pw'     : '',
            'tts_host'      : 'local',
            'tts_port'      : 8300,
            'tts_locale'    : 'de',
            'tts_voice'     : 'de',
            'tts_engine'    : 'espeak',
            'tts_speed'     : 150,
            'tts_pitch'     : 38,
           }

CLIENT_NAME = 'Zamia AI MQTT Server'

# state

do_listen       = True
do_asr          = True
attention       = 0
listening       = True
do_rec          = False
att_force       = False
pstr            = '***'
hstr            = ''
astr            = ''
audio_cnt       = 0
old_state       = None

# audio recording state

audiofns = {}   # location -> str,  path to current wav file being written
wfs      = {}   # location -> wave, current wav file being written

#
# MQTT
#

def on_connect(client, userdata, flag, rc):
    if rc==0:
        logging.info("connected OK Returned code=%s" % repr(rc))
        client.subscribe(TOPIC_INPUT_TEXT)
        client.subscribe(TOPIC_INPUT_AUDIO)
        client.subscribe(TOPIC_CONFIG)
        client.subscribe(TOPIC_RESPONSE)
    else:
        logging.error("Bad connection Returned code=%s" % repr(rc))

def publish_state(client):

    global attention, pstr, hstr, astr, listening, msg_cond, old_state

    msg_cond.acquire()
    try:
        data = {}

        data['attention'] = attention
        data['pstr']      = pstr
        data['hstr']      = hstr
        data['astr']      = astr
        data['listening'] = listening

        if data != old_state:
            logging.debug ('publish_state: %s' % repr(data))
            client.publish(TOPIC_STATE, json.dumps(data))
            old_state = data

    finally:
        msg_cond.release()

def on_message(client, userdata, message):

    # global kernal, lang
    global msg_queue, msg_cond, ignore_audio_before
    global wfs, vf_login, rec_dir, audiofns, pstr, hstr, astr, audio_cnt
    global do_listen, do_rec, do_asr, att_force, listening, attention
    global tts_lock, tts

    # logging.debug( "message received %s" % str(message.payload.decode("utf-8")))
    # logging.debug( "message topic=%s" % message.topic)
    # logging.debug( "message qos=%s" % message.qos)
    # logging.debug( "message retain flag=%s" % message.retain)

    msg_cond.acquire()
    try:

        if message.topic == TOPIC_INPUT_AUDIO:

            data          = json.loads(message.payload)
            data['topic'] = message.topic
            audio         = data['pcm']
            loc           = data['loc']
            do_finalize   = data['final']
            ts            = dateutil.parser.parse(data['ts'])
            
            # ignore old audio recordings that may have lingered in the message queue

            age = (datetime.datetime.now() - ts).total_seconds()
            if age > MAX_AUDIO_AGE:
                # logging.debug ("   ignoring audio that is too old: %fs > %fs" % (age, MAX_AUDIO_AGE))
                return

            if ts < ignore_audio_before:
                # logging.debug ("   ignoring audio that is ourselves talking: %s < %s" % (unicode(ts), unicode(ignore_audio_before)))
                return

            audio_cnt += 1
            pstr = '.' * (audio_cnt/10 + 1)

            if do_rec:

                # store recording in WAV format

                if not loc in wfs:
                    wfs[loc] = None

                if not wfs[loc]:

                    ds = datetime.date.strftime(datetime.date.today(), '%Y%m%d')
                    audiodirfn = '%s/%s-%s-rec/wav' % (rec_dir, vf_login, ds)
                    logging.debug('audiodirfn: %s' % audiodirfn)
                    misc.mkdirs(audiodirfn)

                    cnt = 0
                    while True:
                        cnt += 1
                        audiofns[loc] = '%s/de5-%03d.wav' % (audiodirfn, cnt)
                        if not os.path.isfile(audiofns[loc]):
                            break

                    logging.debug('audiofn: %s' % audiofns[loc])

                    # create wav file 

                    wfs[loc] = wave.open(audiofns[loc], 'wb')
                    wfs[loc].setnchannels(1)
                    wfs[loc].setsampwidth(2)
                    wfs[loc].setframerate(SAMPLE_RATE)

                packed_audio = struct.pack('%sh' % len(audio), *audio)
                wfs[loc].writeframes(packed_audio)

                if do_finalize:

                    afn_parts = audiofns[loc].split('/')

                    pstr = afn_parts[len(afn_parts)-1]
                    logging.info('audiofn %s written.' % audiofns[loc])

                    wfs[loc].close()  
                    wfs[loc] = None

            else:
                audiofns[loc] = ''
                if do_finalize:
                        pstr = '***'

            if do_finalize:
                audio_cnt = 0

            if do_asr:

                msg_queue.append(data)
                msg_cond.notify_all()

            else:
                if do_rec:
                    attention = 30

            publish_state(client)
                
        elif message.topic == TOPIC_INPUT_TEXT:

            data          = json.loads(message.payload)
            data['topic'] = message.topic

            msg_queue.append(data)
            msg_cond.notify_all()
            # print data

        elif message.topic == TOPIC_RESPONSE:

            msg = json.loads(message.payload)

            if msg['utt']:

                tts_lock.acquire()
                try:
                    logging.debug('tts.say...')
                    tts.say(msg['utt'])
                    logging.debug('tts.say finished.')

                except:
                    logging.error('TTS EXCEPTION CAUGHT %s' % traceback.format_exc())
                finally:
                    tts_lock.release()

                ignore_audio_before = datetime.datetime.now() + datetime.timedelta(seconds=AUDIO_EXTRA_DELAY)

            listening = True
            publish_state(client)

        elif message.topic == TOPIC_CONFIG:

            logging.debug( "message received %s" % str(message.payload.decode("utf-8")))
            logging.debug( "message topic=%s" % message.topic)
            logging.debug( "message qos=%s" % message.qos)
            logging.debug( "message retain flag=%s" % message.retain)

            data = json.loads(message.payload)

            do_listen  = data['listen']
            do_rec     = data['record']
            do_asr     = data['asr']
            att_force2 = data['att']
            if att_force2:
                attention = 30
                att_force = True
            elif att_force:
                attention = 2
                att_force = False

            publish_state(client)

    except:
        logging.error('EXCEPTION CAUGHT %s' % traceback.format_exc())

    finally:
        msg_cond.release()

#
# init
#

misc.init_app(PROC_TITLE)

#
# config, cmdline
#

config = misc.load_config('.airc', defaults = DEFAULTS)

broker_host                    = config.get   ('mqtt', 'broker_host')
broker_port                    = config.getint('mqtt', 'broker_port')
broker_user                    = config.get   ('mqtt', 'broker_user')
broker_pw                      = config.get   ('mqtt', 'broker_pw')

ai_model                       = config.get      ('server', 'model')
lang                           = config.get      ('server', 'lang')
vf_login                       = config.get      ('server', 'vf_login')
rec_dir                        = config.get      ('server', 'rec_dir')
kaldi_model_dir                = config.get      ('server', 'kaldi_model_dir')
kaldi_model                    = config.get      ('server', 'kaldi_model')
kaldi_acoustic_scale           = config.getfloat ('server', 'kaldi_acoustic_scale') 
kaldi_beam                     = config.getfloat ('server', 'kaldi_beam') 
kaldi_frame_subsampling_factor = config.getint   ('server', 'kaldi_frame_subsampling_factor') 

tts_host                       = config.get   ('tts',    'tts_host')
tts_port                       = config.getint('tts',    'tts_port')
tts_locale                     = config.get   ('tts',    'tts_locale')
tts_voice                      = config.get   ('tts',    'tts_voice')
tts_engine                     = config.get   ('tts',    'tts_engine')
tts_speed                      = config.getint('tts',    'tts_speed')
tts_pitch                      = config.getint('tts',    'tts_pitch')

all_modules                    = list(map (lambda m: m.strip(), config.get('semantics', 'modules').split(',')))
db_url                         = config.get('db', 'url')

#
# commandline
#

parser = OptionParser("usage: %prog [options]")

parser.add_option ("-v", "--verbose", action="store_true", dest="verbose",
                   help="verbose output")

parser.add_option ("-H", "--host", dest="host", type = "string", default=broker_host,
                   help="MQTT broker host, default: %s" % broker_host)

parser.add_option ("-p", "--port", dest="port", type = "int", default=broker_port,
                   help="MQTT broker port, default: %d" % broker_port)

(options, args) = parser.parse_args()

if options.verbose:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

#
# setup DB, AI Kernal, context
#

db     = LogicDB(db_url)
kernal = AIKernal(db=db, all_modules=all_modules, load_all_modules=True)
kernal.setup_tf_model (mode='decode', load_model=True, ini_fn=ai_model)

user_uri = USER_PREFIX + AI_USER
current_ctx = kernal.find_prev_context (user_uri)

if current_ctx:
    logging.debug ('current_ctx: %s, user: %s' % (current_ctx.name, user_uri))
else:
    logging.warn ('no current_ctx found for user %s ' % user_uri)


#
# TTS
#

tts = TTS (host_tts = tts_host, port_tts = tts_port, locale=tts_locale, voice=tts_voice, engine=tts_engine, speed=tts_speed, pitch=tts_pitch)
ignore_audio_before = datetime.datetime.now()
tts_lock = Lock()

#
# ASR
#

asr = ASR(engine = ASR_ENGINE_NNET3, model_dir = kaldi_model_dir, model_name = kaldi_model,
          kaldi_beam = kaldi_beam, kaldi_acoustic_scale = kaldi_acoustic_scale,
          kaldi_frame_subsampling_factor = kaldi_frame_subsampling_factor)

#
# multithreading: queue, state lock
#

msg_queue = []
msg_cond  = Condition()

#
# mqtt connect
#

logging.debug ('connection to MQTT broker %s:%d ...' % (broker_host, broker_port))

client = mqtt.Client()
client.username_pw_set(broker_user, broker_pw)
client.on_message=on_message
client.on_connect=on_connect

connected = False
while not connected:
    try:

        client.connect(broker_host, port=broker_port, keepalive=10)

        connected = True

    except:
        logging.error('connection to %s:%d failed. retry in %d seconds...' % (broker_host, broker_port, RETRY_DELAY))
        time.sleep(RETRY_DELAY)

logging.debug ('connection to MQTT broker %s:%d ... connected.' % (broker_host, broker_port))

#
# main loop - count down attention, publish state while >0
#

logging.info ('READY.')
logging.info ('main loop starts')

client.loop_start()

att_ts = time.time()

while True:

    try:
        data = None
        msg_cond.acquire()
        try:
            if not msg_queue:
                msg_cond.wait(1.0)
            if msg_queue:
                data = msg_queue.pop(0)
        finally:
            msg_cond.release()

        if data:

            if data['topic'] == TOPIC_INPUT_AUDIO:

                audio       = data['pcm']
                do_finalize = data['final']
                loc         = data['loc']

                logging.debug ('asr.decode...')
                hstr2, confidence = asr.decode(SAMPLE_RATE, audio, do_finalize, stream_id=loc)
                logging.debug ('asr.decode...done')

                if do_finalize:

                    logging.info ( "asr: %9.5f %s" % (confidence, hstr))

                    if hstr2:
                        
                        hstr = hstr2
                        astr = '...'
                        data = {}

                        data['lang'] = lang
                        data['utt']  = hstr
                        data['user'] = AI_USER
                     
                        client.publish(TOPIC_INPUT_TEXT, json.dumps(data))
                        listening = False

                        publish_state(client)

            elif data['topic'] == TOPIC_INPUT_TEXT:

                # print data

                utt      = data['utt']
                lang     = data['lang']
                user_uri = USER_PREFIX + data['user']

                if kernal.nlp_model.lang != lang:
                    logging.warn('incorrect language for model: %s' % lang)
                else:

                    score, resps, actions, solutions, current_ctx = kernal.process_input(utt, kernal.nlp_model.lang, user_uri, prev_ctx=current_ctx)
                    logging.debug ('current_ctx: %s, user: %s' % (current_ctx.name, user_uri))

                    # for idx in range (len(resps)):
                    #     logging.debug('[%05d] %s ' % (score, u' '.join(resps[idx])))

                    # if we have multiple responses, pick one at random

                    do_publish = attention>0

                    if len(resps)>0:

                        idx = random.randint(0, len(resps)-1)

                        # apply DB overlay, if any
                        ovl = solutions[idx].get(ASSERT_OVERLAY_VAR_NAME)
                        if ovl:
                            ovl.do_apply(AI_SERVER_MODULE, kernal.db, commit=True)

                        msg_cond.acquire()
                        try:

                            # refresh attention span on each new interaction step
                            if attention>0:
                                attention = ATTENTION_SPAN

                            acts = actions[idx]
                            for action in acts:
                                logging.debug("ACTION %s" % repr(action))
                                if len(action) == 2 and action[0] == u'attention':
                                    if action[1] == u'on':
                                        attention = ATTENTION_SPAN
                                    else:
                                        attention = 1
                                    do_publish = True
                                if attention>0 and len(action) == 3 and action[0] == u'media':
                                    attention = 1
                                    do_publish = True

                        finally:
                            msg_cond.release()

                        resp = resps[idx]
                        logging.debug('RESP: [%05d] %s' % (score, u' '.join(resps[idx])))

                        msg = {'utt': u' '.join(resp), 'score': score, 'lang': lang}

                    else:
                        logging.error(u'no solution found for input %s' % utt)

                        msg = {'utt': u'', 'score': 0.0, 'lang': lang}
                        acts = []

                    if do_publish:
                        (rc, mid) = client.publish(TOPIC_RESPONSE, json.dumps(msg))
                        logging.info("%s (att: %2d) : %s" % (TOPIC_RESPONSE, attention, json.dumps(msg)))
                        for act in acts:
                            (rc, mid) = client.publish(TOPIC_INTENT, json.dumps(act))
                            logging.info("%s (att: %2d): %s" % (TOPIC_INTENT, attention, json.dumps(act)))
                    else:
                        listening = True

                    # generate astr

                    astr = msg['utt']
                    if acts:
                        if astr:
                            astr += ' - '
                    for action in acts:
                        astr += repr(action)

                    publish_state(client)

        #
        # attention span
        #

        att_delay = time.time()-att_ts
        if att_delay >= 1.0:
            msg_cond.acquire()
            try:
                if attention>0:
                    if not att_force:
                        attention -= 1
                        logging.debug ('decreased attention: %d' % attention)
                        publish_state(client)
            finally:
                msg_cond.release()
            att_ts = time.time()

    except:
        logging.error('EXCEPTION CAUGHT %s' % traceback.format_exc())

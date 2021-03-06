import logging
import json
import os
import re
import requests
import smtplib
import time
import yaml
from datetime import datetime
from threading import Thread
from Queue import Queue
from email.mime.text import MIMEText

COLORS = ['white', 'blue']
SIZES = ['32', '64']

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s: %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

with open('conf.yaml') as conf_file:
    CONF = yaml.load(conf_file)


class Model(object):

    __url_base__ = 'https://play.google.com/store/devices/details/Nexus_6_{size}GB_{color_name}?id=nexus_6_{color}_{size}gb'
    color_map = {
        'white': 'Cloud_White',
        'blue': 'Midnight_Blue'
    }

    def __init__(self, color, size):
        self.name = '{0}{1}'.format(color, size)
        self.color = color
        self.size = size
        self.url = self.__url_base__.format(size=size, color=color, color_name=self.color_map[color])
        logger.info("Created model {0}".format(self))

    def __repr__(self):
        return self.name


class Subscriber(object):

    def __init__(self, email, models, interval=0):
        self.email = email
        self.interval = interval
        self.models = models
        self.notifications = {}
        for model in models:
            self.notifications[model] = None
        logger.info("Created a subscriber {0}".format(self))

    def __repr__(self):
        return "{0}: {1}".format(self.email, self.models)


class ModelMonitor(object):

    __timeout__ = 5
    smtp_from = CONF['smtp_from']
    smtp_password = CONF['smtp_password']

    def __init__(self, model, subscribers, model_stats):
        self.model = model
        self.stats = model_stats
        self.subscribers = []

        # Filter out members we do not care about
        for sub in subscribers:
            if self.model in sub.models:
                self.subscribers.append(sub)
        logger.info("Monitoring {0} with {1} subscribers".format(self.model.name, len(self.subscribers)))

    def check(self):
        if 'test_mode' in CONF and CONF['test_mode']:
            self.notify()
            return

        try:
            r = requests.get(self.model.url, timeout=self.__timeout__)

            if not r.status_code == 200:
                logger.warning("{0}: Invalid code: {1}".format(self.model.name, r.status_code))
                return

            if re.search('We are out of inventory', r.text):
                logger.debug("{0}: Out of stock".format(self.model.name))
                return

            r_size = len(r.text)
            if r_size < 2048:
                logger.warning("{0}: smaller than expected response size: {1}".format(self.model.name, r_size))
                return

            self.notify()

        except Exception, e:
            logger.info("{0}: {1} Exception".format(self.model, type(e)))

    def notify(self):
        print "#" * 110
        print "{0} in stock: {1}".format(self.model.name, self.model.url)
        print "#" * 110

        self.stats.append(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

        for sub in self.subscribers:
            last_time = sub.notifications[self.model]

            if last_time is None:
                logger.debug("Sending first notification to {0}".format(sub.email))
                self.send_notification(sub)

            # Check notification interval
            else:
                now = datetime.now()
                delta = now - last_time
                logger.debug("Sending repeated notification to {0}, {1} seconds since last".format(sub.email, delta.seconds))

                if sub.interval > 0 and delta.seconds >= sub.interval:
                    self.send_notification(sub)

                else:
                    logger.debug("Skipping notification")

    def send_notification(self, sub):
        logger.info("{1}: Notifying {0}".format(sub.email, self.model.name))
        # Reset the subs last notification time
        sub.notifications[self.model] = datetime.now()

        # Create the message
        subject = "{0} is (possibly) in stock!".format(self.model.name, sub.email)
        msg = MIMEText(self.model.url)
        msg['Subject'] = subject
        msg['From'] = self.smtp_from
        msg['To'] = sub.email

        # Send email
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                s = smtplib.SMTP_SSL(CONF['smtp_host'])
                s.login(self.smtp_from, self.smtp_password)
                s.sendmail(self.smtp_from, sub.email, msg.as_string())
                s.quit()
                return
            except Exception:
                logger.warning("Unable to send email {0}/{1}".format(attempt, max_attempts))
                time.sleep(5 * attempt)
            finally:
                attempt += 1


class Monitor(object):

    __request_sleep__ = 5
    __notify_interval__ = 10

    def __init__(self):
        self._create_models()
        self._create_subs()
        self._load_stats()
        self._monitor()

    def _create_models(self):
        self.models = []
        for color in COLORS:
            for size in SIZES:
                m = Model(color, size)
                self.models.append(m)

    def _create_subs(self):
        self.subscribers = []
        with open('subscribers.yaml') as subs_file:
            sub_conf = yaml.load(subs_file)

        for sub_email, sub_data in sub_conf.items():
            sub_interval = 0
            sub_models = self.models

            if not sub_data is None:
                for attr in ['color', 'size']:
                    if attr in sub_data:
                        sub_models = filter(lambda m: getattr(m, attr) == str(sub_data[attr]), sub_models)

                if 'interval' in sub_data:
                    sub_interval = sub_data['interval']

            sub = Subscriber(sub_email, sub_models, sub_interval)
            self.subscribers.append(sub)

    def _dump_stats(self):
        if 'stats_file' in CONF:
            with open(CONF['stats_file'], 'w') as stats_file:
                stats_file.write(json.dumps(self.model_stats))

    def _load_stats(self):
        if 'stats_file' in CONF:
            stats_file_path = CONF['stats_file']
            if os.path.exists(stats_file_path):
                with open(stats_file_path, 'r') as stats_file:
                    self.model_stats = json.loads(stats_file.read())
            else:
                self.model_stats = dict.fromkeys(map(lambda x: x.name, self.models), [])

    def _worker(self):
        model = self.queue.get()
        model_stats = self.model_stats[model.name]
        mm = ModelMonitor(model, self.subscribers, model_stats)
        while True:
            mm.check()
            time.sleep(self.__request_sleep__)

    def _monitor(self):
        self.queue = Queue()
        for i in range(len(self.models)):
            worker = Thread(target=self._worker)
            worker.daemon = True
            worker.start()

        for model in self.models:
            self.queue.put(model)

        try:
            i = 0
            while True:
                i += 1
                time.sleep(1)
                if i % 5 == 0:
                    self._dump_stats()
        except Exception:
            self._dump_stats()
            raise

Monitor()

import json
import logging
import psutil
import subprocess
import os
import yaml
import random
import string
import time

from flask import Flask
from flask import request
from sqldb import IndexesSQL
from elasticsearchE16 import ElasticsearchE16

from two1.commands.util import config
from two1.wallet.two1_wallet import Wallet
from two1.bitserv.flask import Payment
from two1.bitrequests import BitTransferRequests
requests = BitTransferRequests(Wallet(), config.Config().username)

app = Flask(__name__)
# app.debug = True

# Config options
ES_HOSTS = ["172.17.0.2:9200"]
CONTACT = "james@esixteen.co"
EXPIRE_DAYS_FOR_INDEX = 30

# setup wallet
wallet = Wallet()
payment = Payment(app, wallet)

# hide logging
logger = logging.getLogger('werkzeug')

# Create the Database connection object
sql = IndexesSQL()

# Create the ES connection object
es = ElasticsearchE16(ES_HOSTS)


@app.route('/manifest')
def manifest():
    """Provide the app manifest to the 21 crawler."""
    with open('./manifest.yaml', 'r') as f:
        manifest = yaml.load(f)
    return json.dumps(manifest)


@app.route('/indexes', methods=['POST'])
@payment.required(50000)
def index_create():
    """
    Create a new index in ES and set the expire date to X days in the future.
    """
    try:
        # First create the actual index in ES
        index_name = ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(20))
        es.create_index(index_name)

        # Next save it to the db with expire date
        index_expire_time = time.time() + (60 * 60 * 24 * EXPIRE_DAYS_FOR_INDEX)
        sql.insert_new_index(index_name, index_expire_time)

        # Return success
        return json.dumps({
            "success": True,
            "indexId": index_name,
            "indexExpireTime": index_expire_time,
            "indexExpireDisplay": time.ctime(index_expire_time),
            "expired": False
        })

    except Exception as err:
        logger.error("Failure: {0}".format(err))
        return json.dumps(
            {
                "success": False,
                "error": "Error Creating Index: {0} - Please contact {1} for details.".format(err, CONTACT)
            }), 500


@app.route('/index/<index_name>')
@payment.required(10)
def index_status(index_name):
    """
    Gets the status for the specified ES index.
    """
    try:
        # Get the record from the db
        idx_db = sql.get_index(index_name)
        idx_es_exists = es.index_exists(index_name)
        if idx_db is not None and idx_es_exists is True:

            # Check if the index was marked as deleted
            if idx_db[IndexesSQL.DELETED] > 0:
                return json.dumps({"success": False, "error": "Index was previously deleted."}), 500

            # Return the info about the index
            return json.dumps({
                "success": True,
                "indexId": index_name,
                "indexExpireTime": idx_db[IndexesSQL.ID],
                "indexExpireDisplay": time.ctime(idx_db[IndexesSQL.ID]),
                "expired": idx_db[IndexesSQL.ID] > time.time()
            })

        else:
            return json.dumps(
                {
                    "success": False,
                    "error": "Index ID: {0} was not found.".format(index_name)
                }), 404

    except Exception as err:
        logger.error("Failure: {0}".format(err))
        return json.dumps(
            {
                "success": False,
                "error": "Error getting index stats: {0}".format(err)
            }), 500


@app.route('/index/<index_name>', methods=['DELETE'])
@payment.required(10)
def index_delete(index_name):
    """
    Marks the index as deleted in the DB.  We will not actually delete the index in ES to allow recovery.

    TODO: There should probably be extra logic to delete old indexes.
    """
    try:
        # Mark index as deleted
        sql.delete_index(index_name, request.remote_addr)

        return json.dumps({
            "success": True,
            "indexId": index_name,
            "message": "Index {} deleted.".format(index_name)
        })

    except Exception as err:
        logger.error("Failure: {0}".format(err))
        return json.dumps(
            {
                "success": False,
                "error": "Error deleting index: {0}".format(err)
            }), 500


@app.route('/index/<index_name>', methods=['PUT'])
@payment.required(10)
def index_renew(index_name):
    """
    Renews the expire date to + 30 days of today or the current date in the DB (whatever is later).
    """
    try:
        # Get the existing index from the db
        idx = sql.get_index(index_name)
        if idx is not None:

            # Check if the index was marked as deleted
            if idx[IndexesSQL.DELETED] > 0:
                return json.dumps({"success": False, "error": "Index was previously deleted."}), 500

            # Calculate the new expire date
            current_time = time.time()
            new_renew_time = current_time + (60 * 60 * 24 * EXPIRE_DAYS_FOR_INDEX)
            if current_time < idx[IndexesSQL.EXPIRE]:
                new_renew_time = idx[IndexesSQL.EXPIRE] + (60 * 60 * 24 * EXPIRE_DAYS_FOR_INDEX)

            # Update new renew time
            sql.update_expire(index_name, new_renew_time)

            # Return success
            return json.dumps({
                "success": True,
                "indexId": index_name,
                "indexExpireTime": new_renew_time,
                "indexExpireDisplay": time.ctime(new_renew_time),
                "expired": False
            })

        else:
            return json.dumps(
                {
                    "success": False,
                    "error": "Index ID: {0} was not found.".format(index_name)
                }), 404

    except Exception as err:
        logger.error("Failure: {0}".format(err))
        return json.dumps(
            {
                "success": False,
                "error": "Error getting index stats: {0}".format(err)
            }), 500


if __name__ == '__main__':
    import click

    @click.command()
    @click.option("-d", "--daemon", default=False, is_flag=True, help="Run in daemon mode.")
    @click.option("-l", "--log", default="ERROR", help="Logging level to use (DEBUG, INFO, WARNING, ERROR, CRITICAL)")
    def run(daemon, log):
        """
        Run the server.
        """
        # Set logging level
        numeric_level = getattr(logging, log.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError('Invalid log level: %s' % log)
        logging.basicConfig(level=numeric_level)

        if daemon:
            pid_file = './elasticsearche16.pid'
            if os.path.isfile(pid_file):
                pid = int(open(pid_file).read())
                os.remove(pid_file)
                try:
                    p = psutil.Process(pid)
                    p.terminate()
                except:
                    pass
            try:
                p = subprocess.Popen(['python3', 'elasticsearchE16-server.py'])
                open(pid_file, 'w').write(str(p.pid))
            except subprocess.CalledProcessError:
                raise ValueError("error starting elasticsearchE16-server.py daemon")
        else:

            logger.info("Server running...")
            app.run(host='0.0.0.0', port=11016)

    run()
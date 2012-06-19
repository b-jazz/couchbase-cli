#!/usr/bin/env python

"""
Unit tests for backup/restore/transfer/pump.
"""

import binascii
import collections
import glob
import logging
import os
import Queue
import select
import simplejson as json
import shutil
import socket
import struct
import tempfile
import threading
import time
import types
import unittest
import BaseHTTPServer

import pump
import pump_transfer
import pump_bfd
import pump_cb
import pump_mc
import pump_tap

import mc_bin_client
import memcacheConstants

from memcacheConstants import *

# TODO: (1) test multiple buckets.
# TODO: (1) test TAP ttl / time-to-live field.
# TODO: (1) test TAP other TAP_FLAG's.
# TODO: (1) test large clusters.
# TODO: (1) test large unbalanced clusters.
# TODO: (1) test server node dying.
# TODO: (1) test server node hiccup.
# TODO: (1) test server not enough disk space.

class MockHTTPServer(BaseHTTPServer.HTTPServer):
    """Subclass that remembers the rest_server; and, SO_REUSEADDR."""

    def __init__(self, host_port, handler, rest_server):
        self.rest_server = rest_server # Instance of MockRESTServer.
        BaseHTTPServer.HTTPServer.__init__(self, host_port, handler)

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        BaseHTTPServer.HTTPServer.server_bind(self)


class MockRESTHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    """Checks that requests match the expected requests."""

    def do_GET(self):
        self.do_request()

    def do_PUT(self):
        self.do_request()

    def do_POST(self):
        self.do_request()

    def do_request(self):
        test = self.server.rest_server.test
        assert test, \
            "missing a test for incoming REST request: " + \
            self.command + " " + self.path

        expects = self.server.rest_server.expects
        assert len(expects) > 0, \
            "expected no more REST requests but received: " + \
            self.command + " " + self.path

        # Unshift the expected request and pre-canned response.
        request, response = expects[0]
        self.server.rest_server.expects = expects[1:]

        # Might be callback-based request handler.
        if (type(request) == types.FunctionType or
            type(request) == types.MethodType):
            return request(self, request, response)

        # Test the expected request.
        assert self.command == request['command']
        assert self.path == request['path'], self.path + " != " + request['path']

        # Might be callback-based response handler.
        if (type(response) == types.FunctionType or
            type(response) == types.MethodType):
            return response(self, request, response)

        # Send the pre-canned response.
        if response['code'] != 200:
            self.send_error(response['code'], response['message'])
        else:
            self.send_response(200)
            self.send_header("Content-Type", 'text/html')
            self.end_headers()
            self.wfile.write(response['message'])


class MockRESTServer(threading.Thread):

    def __init__(self, port):
        threading.Thread.__init__(self)

        self.daemon = True
        self.stop = False
        self.host = "127.0.0.1"
        self.port = port
        self.reset()

    def reset(self, test=None, expects=[]):
        self.test = test
        self.expects = expects

    def host_port(self):
        return self.host + ":" + str(self.port)

    def url(self):
        return "http://" + self.host_port()

    def run(self):
        host_port = ('', self.port)
        httpd = MockHTTPServer(host_port, MockRESTHandler, self)

        while not self.stop:
            try:
                httpd.handle_request()
            except:
                print "  MockRESTServer: exception"
                self.stop = True

        if httpd.socket:
            httpd.socket.close()


mrs = MockRESTServer(18091) # Mock REST / ns_server server.
mrs.start()

mcs = MockRESTServer(18092) # Mock couchDB API server.
mcs.start()

# ------------------------------------------------

class MockMemcachedServer(threading.Thread):

    def __init__(self, port):
        threading.Thread.__init__(self)

        self.daemon = True
        self.stop = False
        self.host = "127.0.0.1"
        self.port = port
        self.backlog = 5
        self.reset()

    def reset(self, test=None):
        self.test = test
        self.sessions = {}
        self.queue = Queue.Queue(1000)

    def host_port(self):
        return self.host + ":" + str(self.port)

    def url(self):
        return "http://" + self.host_port()

    def run(self):
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind(('', self.port))
            self.server.listen(self.backlog)

            while not self.stop:
                client, address = self.server.accept()
                c = MockMemcachedSession(client, address, self)
                self.sessions[len(self.sessions)] = c
                c.start()

        except socket.error, (value, message):
            self.close()
            print "MockServer socket error: ", message
            sys.exit(1)

        self.close()


class MockMemcachedSession(threading.Thread):
    def __init__(self, client, address, server):
        threading.Thread.__init__(self)
        self.daemon = True
        self.server = server
        self.client = client
        self.address = address
        self.loops = 0 # Number of loops without progress.
        self.loops_max = 10
        self.go = threading.Event()

    def log(self, message):
        pass

    def run(self):
        input = [self.client]

        self.loops = 0

        buf = ''

        while (self.client and self.loops < self.loops_max):
            self.log("loops (" + str(self.loops) + ")")
            self.loops = self.loops + 1

            if not buf:
                iready, oready, eready = select.select(input, [], [], 1)
                if len(eready) > 0:
                    return self.close("select eready")
            else:
                iready = input

            if len(iready) > 0:
                self.log("recv...")

                pkt, buf = self.recv(self.client,
                                     memcacheConstants.MIN_RECV_PACKET, buf)
                if not pkt:
                    return self.close("recv no data")

                magic, cmd, keylen, extlen, dtype, vbucket_id, datalen, opaque, cas = \
                    struct.unpack(memcacheConstants.REQ_PKT_FMT, pkt)
                if (magic != memcacheConstants.REQ_MAGIC_BYTE and
                    magic != memcacheConstants.RES_MAGIC_BYTE):
                    raise Exception("unexpected recv magic: " + str(magic))

                data, buf = self.recv(self.client, datalen, buf)

                # print cmd, vbucket_id, extlen, keylen, data, cas, opaque

                self.loops = 0
                self.log("recv done: %s %s" % (cmd, vbucket_id))
                self.server.queue.put((self, pkt + data))
                self.go.wait()
                self.go.clear()

        if self.loops >= self.loops_max:
            return self.close("loops too long")

        return self.close()

    def close(self, msg=None):
        self.log("close: " + (msg or ''))
        if self.client:
            self.client.close()
        self.client = None

        self.server.queue.put((None, None))

    def recv(self, skt, nbytes, buf):
        while len(buf) < nbytes:
            data = None
            try:
                data = skt.recv(max(nbytes - len(buf), 4096))
            except socket.timeout:
                logging.error("error: recv socket.timeout")
            except Exception as e:
                logging.error("error: recv exception: " + str(e))

            if not data:
                return None, ''
            buf += data

        return buf[:nbytes], buf[nbytes:]


mms0 = MockMemcachedServer(18080)
mms0.start()

mms1 = MockMemcachedServer(18081)
mms1.start()

# ------------------------------------------------

class Worker(threading.Thread):

    def __init__(self, target, args=[]):
        threading.Thread.__init__(self, target=target, args=args, group=None)
        self.daemon = True


# ------------------------------------------------

class TestPumpingStationFind(unittest.TestCase):

    def setUp(self):
        self.find = pump.PumpingStation.find_handler

    def test_find_handlers(self):
        self.assertEqual(4, len(pump_transfer.SOURCES))
        self.assertTrue(len(pump_transfer.SINKS) in [4, 5])
        self.assertEqual(pump_tap.TAPDumpSource,
                         self.find(None,
                                   "http://HOST:8091/pools/default",
                                   pump_transfer.SOURCES))
        self.assertEqual(pump_tap.TAPDumpSource,
                         self.find(None,
                                   "http://HOST:8091/",
                                   pump_transfer.SOURCES))
        self.assertEqual(pump_tap.TAPDumpSource,
                         self.find(None,
                                   "http://HOST",
                                   pump_transfer.SOURCES))

    def test_find_couchbase_handlers(self):
        self.assertEqual(pump_tap.TAPDumpSource,
                         self.find(None,
                                   "couchbase://HOST:8091",
                                   pump_transfer.SOURCES))
        self.assertEqual(pump_cb.CBSink,
                         self.find(None,
                                   "couchbase://HOST:8091",
                                   pump_transfer.SINKS))

    def test_find_bfd_handlers(self):
        d = tempfile.mkdtemp()
        self.assertEqual(None,
                         self.find(None, d,
                                   pump_transfer.SOURCES))
        self.assertEqual(pump_bfd.BFDSink,
                         self.find(None, d,
                                   pump_transfer.SINKS))
        os.makedirs(d + "/bucket-foo/node-bar")
        self.assertEqual(None,
                         self.find(None, d,
                                   pump_transfer.SOURCES))
        open(d + "/bucket-foo/node-bar/data-0000.cbb", "w")
        self.assertEqual(pump_bfd.BFDSource,
                         self.find(None, d,
                                   pump_transfer.SOURCES))
        shutil.rmtree(d, ignore_errors=True)

    def test_find_more_handlers(self):
        self.assertEqual(None,
                         self.find(None,
                                   "not-a-real-source",
                                   pump_transfer.SOURCES))
        self.assertEqual(pump_mc.MCSink,
                         self.find(None,
                                   "memcached://HOST:8091",
                                   pump_transfer.SINKS))
        self.assertEqual(pump.StdOutSink,
                         self.find(self,
                                   "stdout:",
                                   pump_transfer.SINKS))


class TestBackupParseSpec(unittest.TestCase):

    def setUp(self):
        mrs.reset()

    def tearDown(self):
        mrs.reset()

    def test_argv_missing(self):
        backup = pump_transfer.Backup()
        self.assertNotEqual(0, backup.main([]))
        self.assertNotEqual(0, backup.main(["cbbackup"]))

    def test_parse_spec(self):
        b = pump_transfer.Backup()

        err, opts, source, backup_dir = \
            b.opt_parse(["cbbackup", "not-a-real-thing://HOST:1234", "2"])
        self.assertEqual(None, err)

        err, opts, source, backup_dir = \
            b.opt_parse(["cbbackup", "http://HOST:1234", "2"])
        self.assertEqual(None, err)
        self.assertEqual("http://HOST:1234", source)
        self.assertEqual("2", backup_dir)

        host, port, user, pswd, path = \
            pump.parse_spec(opts, source, 1313)
        self.assertEqual("HOST", host)
        self.assertEqual("1234", port)
        self.assertEqual(None, user)
        self.assertEqual(None, pswd)
        self.assertEqual("", path)

        err, opts, source, backup_dir = \
            b.opt_parse(["cbbackup", "http://HOST:1234/pools", "2"])
        self.assertEqual(None, err)
        host, port, user, pswd, path = \
            pump.parse_spec(opts, source, 1313)
        self.assertEqual("/pools", path)

        err, opts, source, backup_dir = \
            b.opt_parse(["cbbackup", "http://HOST:1234/pools/default", "2"])
        self.assertEqual(None, err)
        host, port, user, pswd, path = \
            pump.parse_spec(opts, source, 1313)
        self.assertEqual("/pools/default", path)

        err, opts, source, backup_dir = \
            b.opt_parse(["cbbackup", "http://HOST:1234", "2",
                         "--username=Uabc", "--password=P123"])
        self.assertEqual(None, err)
        self.assertEqual("http://HOST:1234", source)
        self.assertEqual("2", backup_dir)

        host, port, user, pswd, path = \
            pump.parse_spec(opts, source, 1313)
        self.assertEqual("HOST", host)
        self.assertEqual("1234", port)
        self.assertEqual("Uabc", user)
        self.assertEqual("P123", pswd)
        self.assertEqual("", path)

        err, opts, source, backup_dir = \
            b.opt_parse(["cbbackup", "http://User:Pswd@HOST:1234", "2"])
        self.assertEqual(None, err)
        self.assertEqual("http://User:Pswd@HOST:1234", source)
        self.assertEqual("2", backup_dir)

        host, port, user, pswd, path = \
            pump.parse_spec(opts, source, 1313)
        self.assertEqual("HOST", host)
        self.assertEqual("1234", port)
        self.assertEqual("User", user)
        self.assertEqual("Pswd", pswd)
        self.assertEqual("", path)

        err, opts, source, backup_dir = \
            b.opt_parse(["cbbackup", "http://User:Pswd@HOST:1234", "2",
                         "--username=Uabc", "--password=P123"])
        self.assertEqual(None, err)
        self.assertEqual("http://User:Pswd@HOST:1234", source)
        self.assertEqual("2", backup_dir)

        host, port, user, pswd, path = \
            pump.parse_spec(opts, source, 1313)
        self.assertEqual("HOST", host)
        self.assertEqual("1234", port)
        self.assertEqual("Uabc", user)
        self.assertEqual("P123", pswd)
        self.assertEqual("", path)


class TestKeyFilter(unittest.TestCase):

    def setUp(self):
        mrs.reset()

    def tearDown(self):
        mrs.reset()

    def test_bad_key_filter(self):
        d = tempfile.mkdtemp()
        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d,
                                          "-k", "((**"])
        self.assertNotEqual(0, rv)
        shutil.rmtree(d, ignore_errors=True)


class TestTAPDumpSourceCheck(unittest.TestCase):

    def setUp(self):
        mrs.reset()

    def tearDown(self):
        mrs.reset()

    def test_check(self):
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200,
                            'message': SAMPLE_JSON_pools_default_buckets })])

        err, opts, source, backup_dir = \
            pump_transfer.Backup().opt_parse(["cbbackup", mrs.url(), "2"])
        self.assertEqual(mrs.url(), source)
        self.assertEqual("2", backup_dir)
        rv, map = pump_tap.TAPDumpSource.check(opts, source)
        self.assertEqual(0, rv)
        self.assertTrue(map is not None)

    def test_check_no_server(self):
        mrs.reset()

        err, opts, source, backup_dir = \
            pump_transfer.Backup().opt_parse(["cbbackup",
                                              "http://localhost:6666666",
                                              "2"])
        rv, map = pump_tap.TAPDumpSource.check(opts, source)
        self.assertNotEqual(0, rv)
        self.assertTrue(map is None)

    def test_check_not_json(self):
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200,
                            'message': "this is not JSON" })])

        err, opts, source, backup_dir = \
            pump_transfer.Backup().opt_parse(["cbbackup", mrs.url(), "2"])
        self.assertEqual(mrs.url(), source)
        self.assertEqual("2", backup_dir)
        rv, map = pump_tap.TAPDumpSource.check(opts, source)
        self.assertNotEqual(0, rv)
        self.assertTrue(map is None)

    def test_check_bad_json(self):
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200,
                            'message': '["this":"is JSON but unexpected"]' })])

        err, opts, source, backup_dir = \
            pump_transfer.Backup().opt_parse(["cbbackup", mrs.url(), "2"])
        self.assertEqual(mrs.url(), source)
        self.assertEqual("2", backup_dir)
        rv, map = pump_tap.TAPDumpSource.check(opts, source)
        self.assertNotEqual(0, rv)
        self.assertTrue(map is None)

    def test_check_multiple_buckets(self):
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200,
                            'message': """[{"name":"a",
                                            "bucketType":"membase",
                                            "nodes":["fake-nodes-data"],
                                            "nodeLocator":"vbucket",
                                            "vBucketServerMap":{"fake":"map"}},
                                           {"name":"b",
                                            "bucketType":"membase",
                                            "nodes":["fake-nodes-data"],
                                            "nodeLocator":"vbucket",
                                            "vBucketServerMap":{"fake":"map"}}]""" })])

        err, opts, source, backup_dir = \
            pump_transfer.Backup().opt_parse(["cbbackup", mrs.url(), "2"])
        self.assertEqual(mrs.url(), source)
        self.assertEqual("2", backup_dir)
        rv, map = pump_tap.TAPDumpSource.check(opts, source)
        self.assertEqual(0, rv)
        self.assertTrue(map is not None)
        self.assertEqual(2, len(map['buckets']))
        self.assertEqual('a', map['buckets'][0]['name'])
        self.assertEqual('b', map['buckets'][1]['name'])

    def test_check_non_membase_bucket_type(self):
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200,
                            'message': """[{"name":"a",
                                            "bucketType":"not-membase-bucket-type",
                                            "nodes":["fake-nodes-data"],
                                            "nodeLocator":"vbucket",
                                            "vBucketServerMap":{"fake":"map"}},
                                           {"name":"b",
                                            "bucketType":"membase",
                                            "nodes":["fake-nodes-data"],
                                            "nodeLocator":"vbucket",
                                            "vBucketServerMap":{"fake":"map"}}]""" })])

        err, opts, source, backup_dir = \
            pump_transfer.Backup().opt_parse(["cbbackup", mrs.url(), "2"])
        self.assertEqual(mrs.url(), source)
        self.assertEqual("2", backup_dir)
        rv, map = pump_tap.TAPDumpSource.check(opts, source)
        self.assertEqual(0, rv)
        self.assertTrue(map is not None)
        self.assertEqual(1, len(map['buckets']))
        self.assertEqual('b', map['buckets'][0]['name'])


class TestBFDSinkCheck(unittest.TestCase):

    def test_check(self):
        d = tempfile.mkdtemp()
        err, opts, source, backup_dir = \
            pump_transfer.Backup().opt_parse(["cbbackup", "1", d])
        self.assertEqual(d, backup_dir)
        rv, map = pump_bfd.BFDSink.check(opts, backup_dir, None)
        self.assertEqual(0, rv)
        os.removedirs(d)

    def test_check_parent_exists(self):
        d = tempfile.mkdtemp()
        dchild = d + "/child"
        err, opts, source, backup_dir = \
            pump_transfer.Backup().opt_parse(["cbbackup", "1", dchild])
        self.assertEqual(dchild, backup_dir)
        rv, map = pump_bfd.BFDSink.check(opts, backup_dir, None)
        self.assertEqual(0, rv)
        os.removedirs(d)

    def test_check_missing(self):
        d = "/dir/no/exist"
        err, opts, source, backup_dir = \
            pump_transfer.Backup().opt_parse(["cbbackup", "1", d])
        self.assertEqual(d, backup_dir)
        rv, map = pump_bfd.BFDSink.check(opts, backup_dir, None)
        self.assertNotEqual(0, rv)


# ------------------------------------------------

class BackupTestHelper(unittest.TestCase):
    """Provides helper methods to check backup files."""

    def expect_backup_contents(self, backup_dir,
                               expected_memcached_stream=None,
                               expected_items=None):
        mock_stdout = MockStdOut()

        t = pump_transfer.Transfer()
        rv = t.main(["cbtransfer", backup_dir, "stdout:", "-t", "1"],
                    opts_etc={"stdout": mock_stdout,
                              "item_visitor": mock_stdout.item_visitor})
        self.assertEqual(0, rv)

        if expected_memcached_stream:
            self.assertEqual(expected_memcached_stream,
                             ''.join(mock_stdout.msgs))

        if expected_items:
            for idx, actual_item in enumerate(mock_stdout.items):
                expected_item = expected_items[idx]
                self.assertTrue(expected_item)

                ecmd, evbucket_id, ekey, eflg, eexp, ecas, eval = \
                    expected_item
                acmd, avbucket_id, akey, aflg, aexp, acas, aval = \
                    actual_item

                self.assertEqual(ecmd, acmd)
                self.assertEqual(evbucket_id, avbucket_id)
                self.assertEqual(str(ekey), str(akey))
                self.assertEqual(eflg, aflg)
                self.assertEqual(eexp, aexp)
                self.assertEqual(ecas, acas)
                self.assertEqual(str(eval), str(aval))

            self.assertEqual(len(expected_items), len(mock_stdout.items))

    def check_cbb_file_exists(self, dir, num=1, num_buckets=1):
        self.assertEqual(num_buckets,
                         len(glob.glob(dir + "/bucket-*")))
        self.assertEqual(num_buckets * num,
                         len(glob.glob(dir + "/bucket-*/node-*")))
        self.assertEqual(num_buckets * num,
                         len(glob.glob(dir + "/bucket-*/node-*/data-0000.cbb")))


class MCTestHelper(unittest.TestCase):
    """Provides memcached binary protocol helper methods."""

    def setUp(self):
        mrs.reset()
        mms0.reset()
        mms1.reset()

    def tearDown(self):
        mrs.reset()
        mms0.reset()
        mms1.reset()

    def json_2_nodes(self):
        j = SAMPLE_JSON_pools_default_buckets
        j = j.replace("HOST0:8091", mrs.host_port())
        j = j.replace("HOST1:8091", mrs.host + ":8091") # Assuming test won't contact 2nd REST server.
        j = j.replace("HOST0:11210", mms0.host_port())
        j = j.replace("HOST1:11210", mms1.host_port())
        j = j.replace("HOST0", mms0.host)
        j = j.replace("HOST1", mms1.host)
        m = json.loads(j)
        m[0]['nodes'][0]['ports']['direct'] = mms0.port
        m[0]['nodes'][1]['ports']['direct'] = mms1.port
        j = json.dumps(m)
        return j

    def parse_msg(self, buf, magic_expected):
        head = buf[:memcacheConstants.MIN_RECV_PACKET]
        data = buf[memcacheConstants.MIN_RECV_PACKET:]
        magic, cmd, keylen, extlen, dtype, vbucket_id, datalen, opaque, cas = \
            struct.unpack(memcacheConstants.REQ_PKT_FMT, head)
        self.assertEqual(magic, magic_expected)

        ext = ''
        key = ''
        val = ''
        if data:
            ext = data[0:extlen]
            key = data[extlen:extlen+keylen]
            val = data[extlen+keylen:]
        return cmd, vbucket_id, ext, key, val, opaque, cas

    def parse_req(self, buf):
        return self.parse_msg(buf, memcacheConstants.REQ_MAGIC_BYTE)

    def parse_res(self, buf):
        return self.parse_msg(buf, memcacheConstants.RES_MAGIC_BYTE)

    def check_auth(self, req, user, pswd):
        self.assertTrue(req)
        cmd, vbucket_id, ext, key, val, opaque, cas = \
            self.parse_req(req)
        self.assertEqual(memcacheConstants.CMD_SASL_AUTH, cmd)
        self.assertEqual(0, vbucket_id)
        self.assertEqual('', ext)
        self.assertEqual('PLAIN', key)
        self.assertEqual('\x00' + user + '\x00' + pswd, val)
        self.assertEqual(0, cas)
        return cmd, vbucket_id, ext, key, val, opaque, cas

    def check_tap_connect(self, req):
        self.assertTrue(req)
        cmd, vbucket_id, ext, key, val, opaque, cas = \
            self.parse_req(req)
        self.assertEqual(memcacheConstants.CMD_TAP_CONNECT, cmd)
        self.assertEqual(0, vbucket_id)

        expect_ext, expect_val = \
            pump_tap.TAPDumpSource.encode_tap_connect_opts({
                memcacheConstants.TAP_FLAG_DUMP: '',
                memcacheConstants.TAP_FLAG_SUPPORT_ACK: '',
                })

        self.assertEqual(expect_ext, ext)
        self.assertTrue(key) # Expecting non-empty TAP name.
        self.assertEqual(expect_val, val)
        self.assertEqual(0, cas)

        return cmd, vbucket_id, ext, key, val, opaque, cas

    def header(self, cmd, vbucket_id, key, val, ext, opaque, cas,
               dtype=0,
               fmt=memcacheConstants.REQ_PKT_FMT,
               magic=memcacheConstants.REQ_MAGIC_BYTE):
        return struct.pack(fmt, magic, cmd,
                           len(key), len(ext), dtype, vbucket_id,
                           len(key) + len(ext) + len(val), opaque, cas)

    def req_header(self, cmd, vbucket_id, key, val, ext, opaque, cas,
                   dtype=0):
        return self.header(cmd, vbucket_id, key, val, ext, opaque, cas,
                           dtype=dtype,
                           fmt=memcacheConstants.REQ_PKT_FMT,
                           magic=memcacheConstants.REQ_MAGIC_BYTE)

    def res_header(self, cmd, vbucket_id, key, val, ext, opaque, cas,
                   dtype=0):
        return self.header(cmd, vbucket_id, key, val, ext, opaque, cas,
                           dtype=dtype,
                           fmt=memcacheConstants.RES_PKT_FMT,
                           magic=memcacheConstants.RES_MAGIC_BYTE)

    def req(self, cmd, vbucket_id, key, val, ext, opaque, cas,
            dtype=0):
        return self.req_header(cmd, vbucket_id, key, val, ext, opaque, cas,
                               dtype=dtype) + ext + key + val

    def res(self, cmd, vbucket_id, key, val, ext, opaque, cas,
            dtype=0):
        return self.res_header(cmd, vbucket_id, key, val, ext, opaque, cas,
                               dtype=dtype) + ext + key + val


# ------------------------------------------------

class TestTAPDumpSource(MCTestHelper, BackupTestHelper):

    def test_close_at_auth(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        w = Worker(target=self.worker_close_at_auth)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d])
        self.assertNotEqual(0, rv)

        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def worker_close_at_auth(self):
        for mms in [mms0, mms1]:
            client, req = mms.queue.get()
            self.assertTrue(req)
            client.close("simulate auth fail by closing conn")
            client.go.set()

    def test_rejected_auth(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        w = Worker(target=self.worker_rejected_auth)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d])
        self.assertNotEqual(0, rv)

        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def worker_rejected_auth(self):
        for mms in [mms0, mms1]:
            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, 'default', '')
            client.client.send(self.res(cmd, ERR_AUTH_ERROR,
                                        '', '', '', opaque, 0))
            client.go.set()

    def test_close_after_auth(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        w = Worker(target=self.worker_close_after_auth)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d])
        self.assertEqual(0, rv)

        self.check_cbb_file_exists(d, num_buckets=0)

        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def worker_close_after_auth(self):
        for mms in [mms0, mms1]:
            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, 'default', '')
            client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
            client.close("simulate failure right after auth")
            client.go.set()

    def test_close_after_TAP_connect(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        w = Worker(target=self.worker_close_after_TAP_connect)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d,
                                          "-x", "max_retry=0"])
        self.assertEqual(0, rv)
        self.check_cbb_file_exists(d, num_buckets=0)

        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def worker_close_after_TAP_connect(self):
        for mms in [mms0, mms1]:
            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, 'default', '')
            client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
            client.go.set()

            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_tap_connect(req)
            client.close("simulate failure right after TAP connect")
            client.go.set()


class TestTAPDumpSourceMutations(MCTestHelper, BackupTestHelper):

    def test_1_mutation(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        w = Worker(target=self.worker_1_mutation)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d])
        self.assertEqual(0, rv)

        # Two BFD files should be created, with 1 item each.
        self.check_cbb_file_exists(d, num=2)
        self.expect_backup_contents(d,
                                    "set a 0 0 1\r\nA\r\n"
                                    "set a 0 0 1\r\nA\r\n")
        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def worker_1_mutation(self):
        # Sends one TAP_MUTATION with an ACK.
        for mms in [mms0, mms1]:
            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, 'default', '')
            client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
            client.go.set()

            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_tap_connect(req)

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, memcacheConstants.TAP_FLAG_ACK, 0, 0, 0)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        123, 'a', 'A', ext, 789, 321))
            client.go.set()

            client, res = mms.queue.get()
            cmd, vbucket_id, ext, key, val, opaque, cas = \
                self.parse_res(res)
            self.assertEqual(CMD_TAP_MUTATION, cmd)
            self.assertEqual(0, vbucket_id)
            self.assertEqual('', ext)
            self.assertEqual('', key)
            self.assertEqual(789, opaque)
            self.assertEqual(0, cas)
            self.assertEqual('', val)

            client.close("close after ack received")
            client.go.set()

    def test_2_mutation(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        w = Worker(target=self.worker_2_mutation)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d,
                                          "-x", "max_retry=0"])
        self.assertEqual(0, rv)
        self.check_cbb_file_exists(d, num=2)
        # 0xfedcba01 == 4275878401, using high numbers to check endianess.
        # 0xffeedd00 == 4293844224
        self.expect_backup_contents(d,
                                    "set a 4275878401 0 1\r\nA\r\n"
                                    "set b 0 4293844224 1\r\nB\r\n"
                                    "set a 4275878401 0 1\r\nA\r\n"
                                    "set b 0 4293844224 1\r\nB\r\n",
                                    [(CMD_TAP_MUTATION, 123, 'a', 0xfedcba01, 0, 321, 'A'),
                                     (CMD_TAP_MUTATION, 1234, 'b', 0, 0xffeedd00, 4321, 'B'),
                                     (CMD_TAP_MUTATION, 123, 'a', 0xfedcba01, 0, 321, 'A'),
                                     (CMD_TAP_MUTATION, 1234, 'b', 0, 0xffeedd00, 4321, 'B')])
        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def worker_2_mutation(self):
        # Sends two TAP_MUTATION's with an ACK on the last.
        for mms in [mms0, mms1]:
            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, 'default', '')
            client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
            client.go.set()

            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_tap_connect(req)

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, 0, 0, 0xfedcba01, 0)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        123, 'a', 'A', ext, 789, 321))

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, memcacheConstants.TAP_FLAG_ACK, 0, 0, 0xffeedd00)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        1234, 'b', 'B', ext, 987, 4321))
            client.go.set()

            client, res = mms.queue.get()
            cmd, vbucket_id, ext, key, val, opaque, cas = \
                self.parse_res(res)
            self.assertEqual(CMD_TAP_MUTATION, cmd)
            self.assertEqual(0, vbucket_id)
            self.assertEqual('', ext)
            self.assertEqual('', key)
            self.assertEqual(987, opaque)
            self.assertEqual(0, cas)
            self.assertEqual('', val)

            client.close("close after ack received")
            client.go.set()

    def test_key_filter_some(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        w = Worker(target=self.worker_2_mutation)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d, "-k", "a"])
        self.assertEqual(0, rv)
        self.check_cbb_file_exists(d, num=2)
        # 0xfedcba01 == 4275878401
        self.expect_backup_contents(d,
                                    "set a 4275878401 0 1\r\nA\r\n"
                                    "set a 4275878401 0 1\r\nA\r\n",
                                    [(CMD_TAP_MUTATION, 123, 'a', 0xfedcba01, 0, 321, 'A'),
                                     (CMD_TAP_MUTATION, 123, 'a', 0xfedcba01, 0, 321, 'A')])
        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def test_key_filter_everything(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        w = Worker(target=self.worker_2_mutation)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d, "-k", "aaa"])
        self.assertEqual(0, rv)
        self.check_cbb_file_exists(d, num=2)
        self.expect_backup_contents(d, "", [])
        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def test_2_mutation_chopped_header(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        self.chop_at = 16 # Header length is 24 bytes.
        w = Worker(target=self.worker_2_chopped)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d,
                                          "-x", "max_retry=0"])
        self.assertEqual(0, rv)

        # Two BFD files should be created, with 1 item each.
        self.check_cbb_file_exists(d, num=2)
        self.expect_backup_contents(d,
                                    "set a 0 0 1\r\nA\r\n"
                                    "set a 0 0 1\r\nA\r\n")
        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def test_2_mutation_chopped_body(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        self.chop_at = 26 # Header length is 24 bytes.
        w = Worker(target=self.worker_2_chopped)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d,
                                          "-x", "max_retry=0,batch_max_size=1"])
        self.assertNotEqual(0, rv)

        # Two BFD files should be created, with 1 item each.
        self.check_cbb_file_exists(d, num=2)

        # We can't depend on deterministic backup when messages are chopped.
        # self.expect_backup_contents(d,
        #                             "set a 0 0 1\r\nA\r\n",
        #                             [(CMD_TAP_MUTATION, 123, 'a', 0, 0, 321, 'A')])
        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def worker_2_chopped(self):
        # Sends two TAP_MUTATION's, but second message is chopped.
        for mms in [mms0, mms1]:
            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, 'default', '')
            client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
            client.go.set()

            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_tap_connect(req)

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, 0, 0, 0, 0)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        123, 'a', 'A', ext, 987, 321))

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, memcacheConstants.TAP_FLAG_ACK, 0, 0, 0)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        1234, 'b', 'B', ext, 789, 4321)[0:self.chop_at])
            client.close("close after sending chopped message")
            client.go.set()

    def test_delete(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        w = Worker(target=self.worker_delete)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d])
        self.assertEqual(0, rv)
        self.check_cbb_file_exists(d, num=2)
        self.expect_backup_contents(d,
                                    "set a 40302010 0 1\r\nA\r\n"
                                    "delete a\r\n"
                                    "set b 0 12345 1\r\nB\r\n"
                                    "set a 40302010 0 1\r\nA\r\n"
                                    "delete a\r\n"
                                    "set b 0 12345 1\r\nB\r\n",
                                    [(CMD_TAP_MUTATION, 123, 'a', 40302010, 0, 321, 'A'),
                                     (CMD_TAP_DELETE, 111, 'a', 0, 0, 333, ''),
                                     (CMD_TAP_MUTATION, 1234, 'b', 0, 12345, 4321, 'B'),
                                     (CMD_TAP_MUTATION, 123, 'a', 40302010, 0, 321, 'A'),
                                     (CMD_TAP_DELETE, 111, 'a', 0, 0, 333, ''),
                                     (CMD_TAP_MUTATION, 1234, 'b', 0, 12345, 4321, 'B')])
        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def worker_delete(self):
        for mms in [mms0, mms1]:
            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, 'default', '')
            client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
            client.go.set()

            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_tap_connect(req)

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, 0, 0, 40302010, 0)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        123, 'a', 'A', ext, 789, 321))

            ext = struct.pack(memcacheConstants.TAP_GENERAL_PKT_FMT,
                              0, 0, 0)
            client.client.send(self.req(CMD_TAP_DELETE,
                                        111, 'a', '', ext, 777, 333))

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, memcacheConstants.TAP_FLAG_ACK, 0, 0, 12345)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        1234, 'b', 'B', ext, 987, 4321))
            client.go.set()

            client, res = mms.queue.get()
            cmd, vbucket_id, ext, key, val, opaque, cas = \
                self.parse_res(res)
            self.assertEqual(CMD_TAP_MUTATION, cmd)
            self.assertEqual(0, vbucket_id)
            self.assertEqual('', ext)
            self.assertEqual('', key)
            self.assertEqual(987, opaque)
            self.assertEqual(0, cas)
            self.assertEqual('', val)

            client.close("close after ack received")
            client.go.set()

    def test_delete_ack(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        w = Worker(target=self.worker_delete_ack)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d])
        self.assertEqual(0, rv)
        self.check_cbb_file_exists(d, num=2)
        self.expect_backup_contents(d,
                                    "set a 40302010 0 1\r\nA\r\n"
                                    "delete a\r\n"
                                    "set a 40302010 0 1\r\nA\r\n"
                                    "delete a\r\n",
                                    [(CMD_TAP_MUTATION, 123, 'a', 40302010, 0, 321, 'A'),
                                     (CMD_TAP_DELETE, 111, 'a', 0, 0, 333, ''),
                                     (CMD_TAP_MUTATION, 123, 'a', 40302010, 0, 321, 'A'),
                                     (CMD_TAP_DELETE, 111, 'a', 0, 0, 333, '')])
        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def worker_delete_ack(self):
        # The last sent message is a TAP_DELETE with TAP_FLAG_ACK.
        for mms in [mms0, mms1]:
            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, 'default', '')
            client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
            client.go.set()

            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_tap_connect(req)

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, 0, 0, 40302010, 0)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        123, 'a', 'A', ext, 789, 321))

            ext = struct.pack(memcacheConstants.TAP_GENERAL_PKT_FMT,
                              0, memcacheConstants.TAP_FLAG_ACK, 0)
            client.client.send(self.req(CMD_TAP_DELETE,
                                        111, 'a', '', ext, 777, 333))
            client.go.set()

            client, res = mms.queue.get()
            cmd, vbucket_id, ext, key, val, opaque, cas = \
                self.parse_res(res)
            self.assertEqual(CMD_TAP_DELETE, cmd)
            self.assertEqual(0, vbucket_id)
            self.assertEqual('', ext)
            self.assertEqual('', key)
            self.assertEqual(777, opaque)
            self.assertEqual(0, cas)
            self.assertEqual('', val)

            client.close("close after ack received")
            client.go.set()

    def test_noop(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        w = Worker(target=self.worker_noop)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d])
        self.assertEqual(0, rv)
        self.check_cbb_file_exists(d, num=2)
        self.expect_backup_contents(d,
                                    "set a 40302010 0 1\r\nA\r\n"
                                    "delete a\r\n"
                                    "set b 0 12345 1\r\nB\r\n"
                                    "set a 40302010 0 1\r\nA\r\n"
                                    "delete a\r\n"
                                    "set b 0 12345 1\r\nB\r\n",
                                    [(CMD_TAP_MUTATION, 123, 'a', 40302010, 0, 321, 'A'),
                                     (CMD_TAP_DELETE, 111, 'a', 0, 0, 333, ''),
                                     (CMD_TAP_MUTATION, 1234, 'b', 0, 12345, 4321, 'B'),
                                     (CMD_TAP_MUTATION, 123, 'a', 40302010, 0, 321, 'A'),
                                     (CMD_TAP_DELETE, 111, 'a', 0, 0, 333, ''),
                                     (CMD_TAP_MUTATION, 1234, 'b', 0, 12345, 4321, 'B')])
        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def worker_noop(self):
        # Has CMD_NOOP's sprinkled amongst the stream.
        for mms in [mms0, mms1]:
            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, 'default', '')
            client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
            client.go.set()

            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_tap_connect(req)

            client.client.send(self.req(memcacheConstants.CMD_NOOP,
                                        111, 'a', '', '', 777, 333))

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, 0, 0, 40302010, 0)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        123, 'a', 'A', ext, 789, 321))

            client.client.send(self.req(memcacheConstants.CMD_NOOP,
                                        111, 'a', '', '', 777, 333))

            ext = struct.pack(memcacheConstants.TAP_GENERAL_PKT_FMT,
                              0, 0, 0)
            client.client.send(self.req(CMD_TAP_DELETE,
                                        111, 'a', '', ext, 777, 333))

            client.client.send(self.req(memcacheConstants.CMD_NOOP,
                                        111, 'a', '', '', 777, 333))

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, memcacheConstants.TAP_FLAG_ACK, 0, 0, 12345)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        1234, 'b', 'B', ext, 987, 4321))
            client.go.set()

            client, res = mms.queue.get()
            cmd, vbucket_id, ext, key, val, opaque, cas = \
                self.parse_res(res)
            self.assertEqual(CMD_TAP_MUTATION, cmd)
            self.assertEqual(0, vbucket_id)
            self.assertEqual('', ext)
            self.assertEqual('', key)
            self.assertEqual(987, opaque)
            self.assertEqual(0, cas)
            self.assertEqual('', val)

            client.close("close after ack received")
            client.go.set()

    def test_tap_cmd_opaque(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        w = Worker(target=self.worker_tap_cmd_opaque)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d])
        self.assertEqual(0, rv)
        self.check_cbb_file_exists(d, num=2)
        self.expect_backup_contents(d,
                                    "set a 40302010 0 1\r\nA\r\n"
                                    "delete a\r\n"
                                    "set b 0 12345 0\r\n\r\n"
                                    "set a 40302010 0 1\r\nA\r\n"
                                    "delete a\r\n"
                                    "set b 0 12345 0\r\n\r\n",
                                    [(CMD_TAP_MUTATION, 123, 'a', 40302010, 0, 321, 'A'),
                                     (CMD_TAP_DELETE, 111, 'a', 0, 0, 333, ''),
                                     (CMD_TAP_MUTATION, 1234, 'b', 0, 12345, 4321, ''),
                                     (CMD_TAP_MUTATION, 123, 'a', 40302010, 0, 321, 'A'),
                                     (CMD_TAP_DELETE, 111, 'a', 0, 0, 333, ''),
                                     (CMD_TAP_MUTATION, 1234, 'b', 0, 12345, 4321, '')])
        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def worker_tap_cmd_opaque(self):
        # Has CMD_TAP_OPAQUE's sprinkled amongst the stream.
        for mms in [mms0, mms1]:
            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, 'default', '')
            client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
            client.go.set()

            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_tap_connect(req)

            client.client.send(self.req(memcacheConstants.CMD_TAP_OPAQUE,
                                        111, 'o0', '', '', 777, 333))

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, 0, 0, 40302010, 0)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        123, 'a', 'A', ext, 789, 321))

            ext = struct.pack(memcacheConstants.TAP_GENERAL_PKT_FMT,
                              0, memcacheConstants.TAP_FLAG_ACK, 0)
            client.client.send(self.req(memcacheConstants.CMD_TAP_OPAQUE,
                                        111, 'o1', '', ext, 888, 444))
            client.go.set()

            client, res = mms.queue.get()
            cmd, vbucket_id, ext, key, val, opaque, cas = \
                self.parse_res(res)
            self.assertEqual(memcacheConstants.CMD_TAP_OPAQUE, cmd)
            self.assertEqual(0, vbucket_id)
            self.assertEqual('', ext)
            self.assertEqual('', key)
            self.assertEqual(888, opaque)
            self.assertEqual(0, cas)
            self.assertEqual('', val)

            ext = struct.pack(memcacheConstants.TAP_GENERAL_PKT_FMT,
                              0, 0, 0)
            client.client.send(self.req(CMD_TAP_DELETE,
                                        111, 'a', '', ext, 777, 333))

            client.client.send(self.req(memcacheConstants.CMD_TAP_OPAQUE,
                                        111, 'o2', '', '', 999, 555))

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, memcacheConstants.TAP_FLAG_ACK, 0, 0, 12345)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        1234, 'b', '', ext, 987, 4321))
            client.go.set()

            client, res = mms.queue.get()
            cmd, vbucket_id, ext, key, val, opaque, cas = \
                self.parse_res(res)
            self.assertEqual(CMD_TAP_MUTATION, cmd)
            self.assertEqual(0, vbucket_id)
            self.assertEqual('', ext)
            self.assertEqual('', key)
            self.assertEqual(987, opaque)
            self.assertEqual(0, cas)
            self.assertEqual('', val)

            client.close("close after ack received")
            client.go.set()

    def test_flush_all(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        w = Worker(target=self.worker_flush_all)
        w.start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d])
        self.assertEqual(0, rv)
        self.check_cbb_file_exists(d, num=2)
        self.expect_backup_contents(d,
                                    "set a 40302010 0 1\r\nA\r\n"
                                    "set a 40302010 0 1\r\nA\r\n",
                                    [(CMD_TAP_MUTATION, 123, 'a', 40302010, 0, 321, 'A'),
                                     (CMD_TAP_MUTATION, 123, 'a', 40302010, 0, 321, 'A')])
        w.join()
        shutil.rmtree(d, ignore_errors=True)

    def worker_flush_all(self):
        for mms in [mms0, mms1]:
            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, 'default', '')
            client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
            client.go.set()

            client, req = mms.queue.get()
            cmd, _, _, _, _, opaque, _ = \
                self.check_tap_connect(req)

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, 0, 0, 40302010, 0)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        123, 'a', 'A', ext, 789, 321))

            # After we send a flush-all, backup ignores the rest of the stream.

            ext = struct.pack(memcacheConstants.TAP_GENERAL_PKT_FMT,
                              0, 0, 0)
            client.client.send(self.req(memcacheConstants.CMD_TAP_FLUSH,
                                        111, 'a', '', ext, 777, 333))

            ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                              0, memcacheConstants.TAP_FLAG_ACK, 0, 0, 12345)
            client.client.send(self.req(CMD_TAP_MUTATION,
                                        1234, 'b', 'B', ext, 987, 4321))


class RestoreTestHelper:

    def setUp(self):
        MCTestHelper.setUp(self)
        BackupTestHelper.setUp(self)

        # Cmds in order of restoration.
        self.restored_cmds = []

        # Map key is cmd key, value is list of item cmds received for that key.
        self.restored_key_cmds = collections.defaultdict(list)

        # Map key is cmd code (ex: CMD_SET), value is integer count.
        self.restored_cmd_counts = collections.defaultdict(int)

    def gen_backup(self,
                   items_per_node=None,
                   expected_backup_stdout=None,
                   json=None,
                   list_mms=None,
                   more_args=[]):
        """Generate a backup file/directory so we can test restore.

           The items is list of lists, with one list per fake,
           mock node in the cluster."""

        if not items_per_node:
            items_per_node = [
                # (cmd_tap, vbucket_id, key, val, flg, exp, cas)
                [(CMD_TAP_MUTATION, 0, 'a', 'A', 0xf1000000, 1000, 8000),
                 (CMD_TAP_MUTATION, 1, 'b', 'B', 0xf1000001, 1001, 8001)],
                [(CMD_TAP_MUTATION, 900, 'x', 'X', 0xfe000000, 9900, 8800),
                 (CMD_TAP_MUTATION, 901, 'y', 'Y', 0xfe000001, 9901, 8801)]
                ]
            # 0xf1000000 == 4043309056
            # 0xfe000000 == 4261412864
            expected_backup_stdout = \
                "set a 4043309056 1000 1\r\nA\r\n" \
                "set b 4043309057 1001 1\r\nB\r\n" \
                "set x 4261412864 9900 1\r\nX\r\n" \
                "set y 4261412865 9901 1\r\nY\r\n"

        if not json:
            json = self.json_2_nodes()

        if not list_mms:
            list_mms = [mms0, mms1]

        self.assertTrue(len(list_mms) <= len(items_per_node))

        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200,
                            'message': json })])

        workers = []
        for idx, items in enumerate(items_per_node):
            workers.append(Worker(target=self.worker_gen_backup,
                                  args=[idx, list_mms[idx], items]))
            workers[-1].start()

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d] + more_args)
        self.assertEqual(0, rv)

        self.check_cbb_file_exists(d, num=2)
        self.expect_backup_contents(d, expected_backup_stdout)

        for w in workers:
            w.join()

        return d, items_per_node, self.flatten_items_per_node(items_per_node)

    def flatten_items_per_node(self, items_per_node):
        flattened = sum(items_per_node, [])

        # Zero out the CAS value, since we currently use SET/ADD.
        # TODO: (1) revisit CAS once we use SET_WITH_META/ADD_WITH_META.
        arr = []

        for item in flattened:
            cmd_tap, vbucket_id, key, val, flg, exp, cas = item
            arr.append((cmd_tap, vbucket_id, key, val, flg, exp, 0))

        return arr

    def worker_gen_backup(self, idx, mms, items,
                          opaque_base=0,
                          bucket='default',
                          bucket_password=''):
        """Represents a memcached server that provides items
           for gen_backup."""

        self.worker_gen_backup_auth(mms, bucket, bucket_password)

        client, req = mms.queue.get()
        cmd, _, _, _, _, opaque, _ = \
            self.check_tap_connect(req)

        for i, item in enumerate(items):
            cmd_tap, vbucket_id, key, val, flg, exp, cas = item
            if cmd_tap == CMD_TAP_MUTATION:
                ext = struct.pack(memcacheConstants.TAP_MUTATION_PKT_FMT,
                                  0, memcacheConstants.TAP_FLAG_ACK, 0, flg, exp)
            elif cmd_tap == CMD_TAP_DELETE:
                ext = struct.pack(memcacheConstants.TAP_GENERAL_PKT_FMT,
                                  0, memcacheConstants.TAP_FLAG_ACK, 0)
            else:
                self.assertTrue(False,
                                "unexpected cmd_tap: " + str(cmd_tap))

            client.client.send(self.req(cmd_tap, vbucket_id, key, val, ext,
                                        i + opaque_base, cas))
            client.go.set()

            client, res = mms.queue.get()
            cmd, vbucket_id, ext, key, val, opaque, cas = \
                self.parse_res(res)
            self.assertEqual(cmd_tap, cmd)
            self.assertEqual(0, vbucket_id)
            self.assertEqual('', ext)
            self.assertEqual('', key)
            self.assertEqual(i + opaque_base, opaque)
            self.assertEqual(0, cas)
            self.assertEqual('', val)

        client.close("close after last ack received")
        client.go.set()

    def worker_gen_backup_auth(self, mms, bucket, bucket_password):
        client, req = mms.queue.get()
        cmd, _, _, _, _, opaque, _ = \
            self.check_auth(req, bucket, bucket_password)
        client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
        client.go.set()

    def reset_mock_cluster(self, rest_msgs=None, json=None):
        if not json:
            json = self.json_2_nodes()

        mrs.reset(self,
                  rest_msgs or
                  [({ 'command': 'GET',
                      'path': '/pools/default/buckets'},
                    { 'code': 200, 'message': json })])
        mms0.reset()
        mms1.reset()

    def worker_restore(self, idx, mms, orig_items_total,
                       bucket='default', bucket_password=''):
        """Represents a mock memcached server during the restore phase
           that just collects all received commands."""

        while len(self.restored_key_cmds) < orig_items_total:
            client, req = mms.queue.get()
            mms.queue.task_done()
            if not client or not req:
                return
            if not self.handle_mc_req(client, req, bucket, bucket_password):
                return

    def handle_mc_req(self, client, req, bucket, bucket_password):
        cmd, vbucket_id, ext, key, val, opaque, cas = \
            self.parse_req(req)
        self.restored_cmd_counts[cmd] += 1

        if cmd == memcacheConstants.CMD_SASL_AUTH:
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, bucket, bucket_password)
        else:
            if (cmd == memcacheConstants.CMD_SET or
                cmd == memcacheConstants.CMD_ADD):
                cmd_tap = CMD_TAP_MUTATION
                flg, exp = struct.unpack(SET_PKT_FMT, ext)
            elif cmd == memcacheConstants.CMD_DELETE:
                cmd_tap = CMD_TAP_DELETE
                flg, exp = 0, 0
            else:
                self.assertTrue(False,
                                "received unexpected restore cmd: " +
                                str(cmd) + " with key: " + key)

            item = (cmd_tap, vbucket_id, key, val, flg, exp, cas)
            self.restored_cmds.append(item)
            self.restored_key_cmds[key].append(item)

        client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
        client.go.set()
        return True

    def check_restore_matches_backup(self, expected_items,
                                     expected_cmd_counts=2,
                                     expected_sasl_counts=2):
        self.assertEqual(len(expected_items),
                         len(self.restored_cmds))
        self.assertEqual(expected_cmd_counts,
                         len(self.restored_cmd_counts))
        self.assertEqual(expected_sasl_counts,
                         self.restored_cmd_counts[CMD_SASL_AUTH])

        before = sorted(expected_items)
        after = sorted(self.restored_cmds)

        # Although we do a deep before and after comparison later,
        # these separate length checks help the humans to debug.
        #
        self.assertEqual(len(before), len(after))

        for i, before_item in enumerate(before):
            self.assertEqual(len(before_item[3]), len(after[i][3]))

        self.assertEqual(before, after)

    def check_restore(self, items_per_node,
                      expected_cmd_counts=2,
                      expected_items=None,
                      threads=1,
                      batch_max_size=1,
                      batch_max_bytes=400000,
                      more_args=[]):
        d, orig_items, orig_items_flattened = \
            self.gen_backup(items_per_node=items_per_node)

        if not expected_items:
            expected_items = orig_items_flattened

        self.reset_mock_cluster()

        # Two mock servers in the cluster.
        workers = [ Worker(target=self.worker_restore,
                           args=[0, mms0, len(orig_items_flattened)]),
                    Worker(target=self.worker_restore,
                           args=[1, mms1, len(orig_items_flattened)]) ]
        for w in workers:
            w.start()

        restore_args = ["cbrestore", d, mrs.url(),
                        "-t", str(threads),
                        "-x",
                        "batch_max_size=%s,batch_max_bytes=%s" %
                        (batch_max_size, batch_max_bytes)] + \
                        more_args

        rv = pump_transfer.Restore().main(restore_args)
        self.assertEqual(0, rv)
        self.check_restore_matches_backup(expected_items,
                                          expected_cmd_counts=expected_cmd_counts)

        self.check_restore_wait_for_workers(workers)
        shutil.rmtree(d, ignore_errors=True)

        return orig_items_flattened

    def check_restore_wait_for_workers(self, workers):
        """Test subclasses may override this method, in case there are more
           complex wait conditions during restore testing."""
        for w in workers:
            w.join()


class TestRestore(MCTestHelper, BackupTestHelper, RestoreTestHelper):

    def setUp(self):
        RestoreTestHelper.setUp(self)

    def test_restore_simple(self):
        source_items = self.check_restore(None)
        self.assertEqual(len(source_items),
                         self.restored_cmd_counts[CMD_SET])

    def test_restore_simple_2threads(self):
        source_items = self.check_restore(None, threads=2)
        self.assertEqual(len(source_items),
                         self.restored_cmd_counts[CMD_SET])

    def test_restore_simple_4threads(self):
        source_items = self.check_restore(None, threads=4)
        self.assertEqual(len(source_items),
                         self.restored_cmd_counts[CMD_SET])

    def test_restore_simple_2batch(self):
        source_items = self.check_restore(None,
                                          batch_max_size=2)
        self.assertEqual(len(source_items),
                         self.restored_cmd_counts[CMD_SET])

    def test_restore_simple_8batch(self):
        source_items = self.check_restore(None,
                                          batch_max_size=8)
        self.assertEqual(len(source_items),
                         self.restored_cmd_counts[CMD_SET])

    def test_restore_simple_4thread_8batch(self):
        source_items = self.check_restore(None,
                                          threads=4,
                                          batch_max_size=8)
        self.assertEqual(len(source_items),
                         self.restored_cmd_counts[CMD_SET])

    def test_restore_big_expirations_and_CAS(self):
        items_per_node = [
            # (cmd_tap, vbucket_id, key, val, flg, exp, cas)
            [(CMD_TAP_MUTATION, 0, 'a', 'A', 0xf1000000, 0xa0001000, 1000 * 0xffffffff),
             (CMD_TAP_MUTATION, 1, 'b', 'B', 0xf1000001, 0xb0001001, 2000 * 0xffffffff)],
            [(CMD_TAP_MUTATION, 900, 'x', 'X', 0xfe000000, 0xc0009900, 10000 * 0xffffffff),
             (CMD_TAP_MUTATION, 901, 'y', 'Y', 0xfe000001, 0xd0009901, 20000 * 0xffffffff)]
            ]

        source_items = self.check_restore(items_per_node)
        self.assertEqual(len(source_items),
                         self.restored_cmd_counts[CMD_SET])

    def test_restore_deletes(self):
        items_per_node = [
            # (cmd_tap, vbucket_id, key, val, flg, exp, cas)
            [(CMD_TAP_MUTATION, 0, 'a', 'A', 0xf1000000, 0xa0001000, 1000 * 0xffffffff),
             (CMD_TAP_MUTATION, 1, 'b', 'B', 0xf1000001, 0xb0001001, 2000 * 0xffffffff),
             (CMD_TAP_DELETE, 0, 'a', '', 0, 0, 3000 * 0xffffffff)
             ],
            [(CMD_TAP_MUTATION, 900, 'x', 'X', 0xfe000000, 0xc0009900, 10000 * 0xffffffff),
             (CMD_TAP_MUTATION, 901, 'y', 'Y', 0xfe000001, 0xd0009901, 20000 * 0xffffffff),
             (CMD_TAP_DELETE, 901, 'y', '', 0, 0, 30000 * 0xffffffff),
             (CMD_TAP_MUTATION, 901, 'y', 'Y-back', 123, 456, 40000 * 0xffffffff)
             ]
            ]

        source_items = self.check_restore(items_per_node,
                                          expected_cmd_counts=3)
        self.assertEqual(5, self.restored_cmd_counts[CMD_SET])
        self.assertEqual(2, self.restored_cmd_counts[CMD_DELETE])
        self.assertEqual(2, len(self.restored_key_cmds['a']))
        self.assertEqual(1, len(self.restored_key_cmds['b']))
        self.assertEqual(1, len(self.restored_key_cmds['x']))
        self.assertEqual(3, len(self.restored_key_cmds['y']))
        self.assertEqual(CMD_TAP_MUTATION, self.restored_key_cmds['a'][0][0])
        self.assertEqual(CMD_TAP_DELETE, self.restored_key_cmds['a'][1][0])
        self.assertEqual(CMD_TAP_MUTATION, self.restored_key_cmds['y'][0][0])
        self.assertEqual(CMD_TAP_DELETE, self.restored_key_cmds['y'][1][0])
        self.assertEqual(CMD_TAP_MUTATION, self.restored_key_cmds['y'][2][0])

    def test_restore_blobs(self, large_blob_size=40000, batch_max_bytes=400000):
        kb = binascii.a2b_hex('00ff010203040506070800')
        vb = kb * 5

        kx = binascii.a2b_hex('0000000000000000000000')
        vx = ''.join(['\x00' for x in xrange(large_blob_size)])

        items_per_node = [
            # (cmd_tap, vbucket_id, key, val, flg, exp, cas)
            [(CMD_TAP_MUTATION, 1, kb, vb, 0, 0, 0)],
            [(CMD_TAP_MUTATION, 900, kx, vx, 0, 0, 1)]
            ]

        source_items = self.check_restore(items_per_node,
                                          expected_cmd_counts=2,
                                          batch_max_bytes=batch_max_bytes)
        self.assertEqual(2, self.restored_cmd_counts[CMD_SET])
        self.assertEqual(1, len(self.restored_key_cmds[kb]))
        self.assertEqual(1, len(self.restored_key_cmds[kx]))
        self.assertEqual(vb, self.restored_key_cmds[kb][0][3])
        self.assertEqual(vx, self.restored_key_cmds[kx][0][3])

    def test_restore_1M_blob(self):
        self.test_restore_blobs(large_blob_size=1 * 1024 * 1024)

    def test_restore_30M_blob(self):
        self.test_restore_blobs(large_blob_size=30 * 1024 * 1024)

    def test_restore_batch_max_bytes(self):
        self.test_restore_blobs(large_blob_size=40000, batch_max_bytes=100)


class TestNotMyVBucketRestore(MCTestHelper, BackupTestHelper, RestoreTestHelper):

    def setUp(self):
        RestoreTestHelper.setUp(self)

        self.reqs_after_respond_with_not_my_vbucket = None

        self.items_per_node = [
            # (cmd_tap, vbucket_id, key, val, flg, exp, cas)
            [(CMD_TAP_MUTATION, 0, 'a', 'A', 0, 0, 1000),
             (CMD_TAP_MUTATION, 1, 'b', 'B', 1, 1, 2000)],
            [(CMD_TAP_MUTATION, 900, 'x', 'X', 900, 900, 10000),
             (CMD_TAP_MUTATION, 901, 'y', 'Y', 901, 901, 20000)]
            ]

    def handle_mc_req(self, client, req, bucket, bucket_password):
        """Sends NOT_MY_VBUCKET to test topology change detection."""

        client.reqs = getattr(client, "reqs", 0) + 1

        cmd, vbucket_id, ext, key, val, opaque, cas = \
            self.parse_req(req)
        self.restored_cmd_counts[cmd] += 1

        if client.reqs >= self.reqs_after_respond_with_not_my_vbucket:
            client.client.send(self.res(cmd, ERR_NOT_MY_VBUCKET,
                                        '', '', '', opaque, 0))
            client.go.set()
            return True

        elif cmd == memcacheConstants.CMD_SASL_AUTH:
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, bucket, bucket_password)
        else:
            if (cmd == memcacheConstants.CMD_SET or
                cmd == memcacheConstants.CMD_ADD):
                cmd_tap = CMD_TAP_MUTATION
                flg, exp = struct.unpack(SET_PKT_FMT, ext)
            elif cmd == memcacheConstants.CMD_DELETE:
                cmd_tap = CMD_TAP_DELETE
                flg, exp = 0, 0
            else:
                self.assertTrue(False,
                                "received unexpected restore cmd: " +
                                str(cmd) + " with key: " + key)

            item = (cmd_tap, vbucket_id, key, val, flg, exp, cas)
            self.restored_cmds.append(item)
            self.restored_key_cmds[key].append(item)

        client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
        client.go.set()
        return True

    def go(self, reqs_after_respond_with_not_my_vbucket,
           threads=4,
           batch_max_size=1):
        d, orig_items, orig_items_flattened = \
            self.gen_backup(items_per_node=self.items_per_node)

        self.reset_mock_cluster()

        self.reqs_after_respond_with_not_my_vbucket = \
            reqs_after_respond_with_not_my_vbucket

        # Two mock servers in the cluster.
        workers = [ Worker(target=self.worker_restore,
                           args=[0, mms0, len(orig_items_flattened)]),
                    Worker(target=self.worker_restore,
                           args=[1, mms1, len(orig_items_flattened)]) ]
        for w in workers:
            w.start()

        rv = pump_transfer.Restore().main(["cbrestore", d, mrs.url(),
                                           "-t", str(threads),
                                           "-x",
                                           "batch_max_size=%s" % (batch_max_size)])
        self.assertNotEqual(0, rv)

        for w in workers:
            w.join()
        shutil.rmtree(d, ignore_errors=True)

    def test_immediate_not_my_vbucket_during_restore(self):
        self.go(2)

    def test_later_not_my_vbucket_during_restore(self):
        self.go(3)

    def test_immediate_not_my_vbucket_during_restore_1T(self):
        self.go(2, threads=1)

    def test_immediate_not_my_vbucket_during_restore_5T(self):
        self.go(2, threads=5)

    def test_immediate_not_my_vbucket_during_restore_5B(self):
        self.go(2, batch_max_size=5)


class TestBackoffRestore(MCTestHelper, BackupTestHelper, RestoreTestHelper):

    def setUp(self):
        RestoreTestHelper.setUp(self)

        self.reqs_after_respond_with_backoff = None

        self.items_per_node = [
            # (cmd_tap, vbucket_id, key, val, flg, exp, cas)
            [(CMD_TAP_MUTATION, 0, 'a', 'A', 0, 0, 1000),
             (CMD_TAP_MUTATION, 1, 'b', 'B', 1, 1, 2000)],
            [(CMD_TAP_MUTATION, 900, 'x', 'X', 900, 900, 10000),
             (CMD_TAP_MUTATION, 901, 'y', 'Y', 901, 901, 20000)]
            ]

    def handle_mc_req(self, client, req, bucket, bucket_password):
        """Sends backoff responses to test retries."""

        client.reqs = getattr(client, "reqs", 0) + 1

        cmd, vbucket_id, ext, key, val, opaque, cas = \
            self.parse_req(req)

        if (self.reqs_after_respond_with_backoff and
            self.reqs_after_respond_with_backoff <= client.reqs):
            self.reqs_after_respond_with_backoff = None
            client.client.send(self.res(cmd, self.backoff_err,
                                        '', '', '', opaque, 0))
            client.go.set()
            return True

        self.restored_cmd_counts[cmd] += 1

        if cmd == memcacheConstants.CMD_SASL_AUTH:
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, bucket, bucket_password)
        else:
            if (cmd == memcacheConstants.CMD_SET or
                cmd == memcacheConstants.CMD_ADD):
                cmd_tap = CMD_TAP_MUTATION
                flg, exp = struct.unpack(SET_PKT_FMT, ext)
            elif cmd == memcacheConstants.CMD_DELETE:
                cmd_tap = CMD_TAP_DELETE
                flg, exp = 0, 0
            else:
                self.assertTrue(False,
                                "received unexpected restore cmd: " +
                                str(cmd) + " with key: " + key)

            item = (cmd_tap, vbucket_id, key, val, flg, exp, cas)
            self.restored_cmds.append(item)
            self.restored_key_cmds[key].append(item)

        client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
        client.go.set()
        return True

    def go(self, reqs_after_respond_with_backoff,
           threads=1,
           batch_max_size=1):
        d, orig_items, orig_items_flattened = \
            self.gen_backup(items_per_node=self.items_per_node)

        self.reset_mock_cluster()

        self.reqs_after_respond_with_backoff = \
            reqs_after_respond_with_backoff

        # Two mock servers in the cluster.
        workers = [ Worker(target=self.worker_restore,
                           args=[0, mms0, len(orig_items_flattened)]),
                    Worker(target=self.worker_restore,
                           args=[1, mms1, len(orig_items_flattened)]) ]
        for w in workers:
            w.start()

        rv = pump_transfer.Restore().main(["cbrestore", d, mrs.url(),
                                           "-t", str(threads),
                                           "-x",
                                           "batch_max_size=%s" % (batch_max_size)])
        self.assertEqual(0, rv)

        for w in workers:
            w.join()
        shutil.rmtree(d, ignore_errors=True)

    def test_etmpfail_during_restore(self):
        self.backoff_err = ERR_ETMPFAIL
        self.go(3)

    def test_earlier_etmpfail_during_restore(self):
        self.backoff_err = ERR_ETMPFAIL
        self.go(2)

    def test_ebusy_during_restore(self):
        self.backoff_err = ERR_EBUSY
        self.go(3)


class TestRejectedSASLAuth(MCTestHelper, BackupTestHelper, RestoreTestHelper):

    def setUp(self):
        RestoreTestHelper.setUp(self)

    def test_rejected_auth(self):
        self.items_per_node = [
            # (cmd_tap, vbucket_id, key, val, flg, exp, cas)
            [(CMD_TAP_MUTATION, 0, 'a', 'A', 0, 0, 1000),
             (CMD_TAP_MUTATION, 1, 'b', 'B', 1, 1, 2000)],
            [(CMD_TAP_MUTATION, 900, 'x', 'X', 900, 900, 10000),
             (CMD_TAP_MUTATION, 901, 'y', 'Y', 901, 901, 20000)]
            ]

        d, orig_items, orig_items_flattened = \
            self.gen_backup(items_per_node=self.items_per_node)

        self.reset_mock_cluster()

        # Two mock servers in the cluster.
        workers = [ Worker(target=self.worker_restore,
                           args=[0, mms0, len(orig_items_flattened)]),
                    Worker(target=self.worker_restore,
                           args=[1, mms1, len(orig_items_flattened)]) ]
        for w in workers:
            w.start()

        rv = pump_transfer.Restore().main(["cbrestore", d, mrs.url()])
        self.assertNotEqual(0, rv)

        for w in workers:
            w.join()
        shutil.rmtree(d, ignore_errors=True)

    def handle_mc_req(self, client, req, bucket, bucket_password):
        cmd, vbucket_id, ext, key, val, opaque, cas = \
            self.parse_req(req)
        self.restored_cmd_counts[cmd] += 1

        if cmd == memcacheConstants.CMD_SASL_AUTH:
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, bucket, bucket_password)
            # Even though cbrestore sent the right SASL AUTH info,
            # let's reject them for testing.
            client.client.send(self.res(cmd, ERR_AUTH_ERROR,
                                        '', '', '', opaque, 0))
            client.go.set()
            return True
        else:
            if (cmd == memcacheConstants.CMD_SET or
                cmd == memcacheConstants.CMD_ADD):
                cmd_tap = CMD_TAP_MUTATION
                flg, exp = struct.unpack(SET_PKT_FMT, ext)
            elif cmd == memcacheConstants.CMD_DELETE:
                cmd_tap = CMD_TAP_DELETE
                flg, exp = 0, 0
            else:
                self.assertTrue(False,
                                "received unexpected restore cmd: " +
                                str(cmd) + " with key: " + key)

            item = (cmd_tap, vbucket_id, key, val, flg, exp, cas)
            self.restored_cmds.append(item)
            self.restored_key_cmds[key].append(item)

        client.client.send(self.res(cmd, 0, '', '', '', opaque, 0))
        client.go.set()
        return True


class TestRestoreAllDeletes(MCTestHelper, BackupTestHelper, RestoreTestHelper):

    def setUp(self):
        RestoreTestHelper.setUp(self)

    def test_restore_all_deletes(self):
        """Test restoring DELETE's against a cluster that doesn't
           have any of the items for attempted DELETION."""

        items_per_node = [
            # (cmd_tap, vbucket_id, key, val, flg, exp, cas)
            [(CMD_TAP_DELETE, 0, 'a', '', 0, 0, 3000 * 0xffffffff)],
            [(CMD_TAP_DELETE, 901, 'y', '', 0, 0, 30000 * 0xffffffff)]
            ]

        source_items = self.check_restore(items_per_node,
                                          expected_cmd_counts=2,
                                          expected_items=[])
        self.assertEqual(2, self.restored_cmd_counts[CMD_DELETE])
        self.assertEqual(1, len(self.restored_key_cmds['a']))
        self.assertEqual(1, len(self.restored_key_cmds['y']))
        self.assertEqual(CMD_TAP_DELETE, self.restored_key_cmds['a'][0][0])
        self.assertEqual(CMD_TAP_DELETE, self.restored_key_cmds['y'][0][0])

    def handle_mc_req(self, client, req, bucket, bucket_password):
        """Sends ERR_KEY_ENOENT for DELETE commands."""

        client.reqs = getattr(client, "reqs", 0) + 1

        cmd, vbucket_id, ext, key, val, opaque, cas = \
            self.parse_req(req)
        self.restored_cmd_counts[cmd] += 1

        status = 0

        if cmd == memcacheConstants.CMD_SASL_AUTH:
            cmd, _, _, _, _, opaque, _ = \
                self.check_auth(req, bucket, bucket_password)
        else:
            if (cmd == memcacheConstants.CMD_SET or
                cmd == memcacheConstants.CMD_ADD):
                cmd_tap = CMD_TAP_MUTATION
                flg, exp = struct.unpack(SET_PKT_FMT, ext)
            elif cmd == memcacheConstants.CMD_DELETE:
                cmd_tap = CMD_TAP_DELETE
                flg, exp = 0, 0
                status = ERR_KEY_ENOENT
            else:
                self.assertTrue(False,
                                "received unexpected restore cmd: " +
                                str(cmd) + " with key: " + key)

            item = (cmd_tap, vbucket_id, key, val, flg, exp, cas)
            self.restored_cmds.append(item)
            self.restored_key_cmds[key].append(item)

        client.client.send(self.res(cmd, status,
                                    '', '', '', opaque, 0))
        client.go.set()
        return True


class TestDesignDocs(MCTestHelper, BackupTestHelper, RestoreTestHelper):

    def setUp(self):
        RestoreTestHelper.setUp(self)
        self.mcs_events = []
        self.mcs_event = threading.Event()
        self.mcs_event.clear()
        mcs.reset()

    def test_ddoc_backup_restore(self):
        source_items = self.check_restore(None)
        self.assertEqual(len(source_items),
                         self.restored_cmd_counts[CMD_SET])

    def gen_backup(self,
                   items_per_node=None,
                   expected_backup_stdout=None,
                   json=None,
                   list_mms=None):
        ddocs_qry = "?startkey=\"_design/\"&endkey=\"_design0\"&include_docs=true"

        mcs.reset(self,
                  [({ 'command': 'GET',
                      'path': '/default/_all_docs' + ddocs_qry },
                    self.on_all_docs)])

        rv = RestoreTestHelper.gen_backup(self,
                                          items_per_node=items_per_node,
                                          expected_backup_stdout=expected_backup_stdout,
                                          json=json,
                                          list_mms=list_mms)

        print "waiting for mcs all_docs..."
        self.mcs_event.wait()
        self.mcs_event.clear()
        print "waiting for mcs all_docs... done"
        self.assertTrue("all_docs" in self.mcs_events)

        return rv

    def json_2_nodes(self):
        json = MCTestHelper.json_2_nodes(self)
        json = json.replace('CAPIk0', 'couchApiBase')
        json = json.replace('CAPIv0', "http://%s/default" % (mcs.host_port()))
        return json

    def on_all_docs(self, req, _1, _2):
        print "on_all_docs", req.command, req.path
        ok = """{"total_rows":1,"offset":0,
                 "rows":[
                  {"id":"_design/dev_dd0",
                   "key":"_design/dev_dd0",
                   "value":{"rev":"7-aa4defd3"},
                   "doc":{
                     "_id":"_design/dev_dd0",
                     "_rev":"7-aa4defd3",
                     "views":{
                       "view0":{
                         "map":"function (doc) {\\n  emit(doc._id, null);\\n}"
                       }
                     }
                   }}]}"""
        req.send_response(200)
        req.send_header("Content-Type", 'application/json')
        req.end_headers()
        req.wfile.write(ok)

        self.mcs_events.append("all_docs")
        self.mcs_event.set()

    def reset_mock_cluster(self):
        print "reset_mock_cluster..."
        mcs.reset(self,
                  [({ 'command': 'PUT',
                      'path': '/default/_design/dev_dd0' },
                    self.on_ddoc_put)])
        RestoreTestHelper.reset_mock_cluster(self)
        print "reset_mock_cluster... done"

    def on_ddoc_put(self, req, _1, _2):
        print "on_ddoc_put..."
        ok = """{"ok":true,
                 "id":"_design/example",
                 "rev":"1-230141dfa7e07c3dbfef0789bf11773a"}"""
        req.send_response(200)
        req.send_header("Content-Type", 'application/json')
        req.end_headers()
        req.wfile.write(ok)

        time.sleep(0.01) # See: http://stackoverflow.com/questions/383738

        self.mcs_events.append("ddocs_put")
        self.mcs_event.set()
        print "on_ddoc_put... done"

    def check_restore_wait_for_workers(self, workers):
        RestoreTestHelper.check_restore_wait_for_workers(self, workers)

        print "waiting for mcs ddocs_put..."
        self.mcs_event.wait()
        self.mcs_event.clear()
        print "waiting for mcs ddocs_put... done"
        self.assertTrue("ddocs_put" in self.mcs_events)


class TestBackupDryRun(MCTestHelper, BackupTestHelper):

    def test_dry_run(self):
        d = tempfile.mkdtemp()
        mrs.reset(self, [({ 'command': 'GET',
                            'path': '/pools/default/buckets'},
                          { 'code': 200, 'message': self.json_2_nodes() })])

        rv = pump_transfer.Backup().main(["cbbackup", mrs.url(), d,
                                          "--dry-run"])
        self.assertEqual(0, rv)

        self.assertEqual(0, len(glob.glob(d + "/bucket-*")))
        self.assertEqual(0, len(glob.glob(d + "/bucket-*/design.json")))
        self.assertEqual(0, len(glob.glob(d + "/bucket-*/node-*")))
        self.assertEqual(0, len(glob.glob(d + "/bucket-*/node-*/data-*.cbb")))

        shutil.rmtree(d, ignore_errors=True)


class TestCBBMaxSize(MCTestHelper, BackupTestHelper, RestoreTestHelper):

    def setUp(self):
        RestoreTestHelper.setUp(self)

    def gen_backup(self,
                   items_per_node=None,
                   expected_backup_stdout=None,
                   json=None,
                   list_mms=None,
                   more_args=[]):
        more_args = more_args + ["-x", "cbb_max_mb=0.0000001,batch_max_size=1"]
        return RestoreTestHelper.gen_backup(self,
                                            items_per_node=items_per_node,
                                            expected_backup_stdout=expected_backup_stdout,
                                            json=json,
                                            list_mms=list_mms,
                                            more_args=more_args)

    def test_cbb_max_size(self):
        source_items = self.check_restore(None)
        self.assertEqual(len(source_items),
                         self.restored_cmd_counts[CMD_SET])

    def check_cbb_file_exists(self, d, num=1):
        self.assertEqual(1, len(glob.glob(d + "/bucket-*")))
        self.assertEqual(2, len(glob.glob(d + "/bucket-*/node-*")))
        self.assertEqual(4, len(glob.glob(d + "/bucket-*/node-*/data-*.cbb")))


# ------------------------------------------------------

SAMPLE_JSON_pools = """
{"pools":[{"name":"default",
           "uri":"/pools/default",
           "streamingUri":"/poolsStreaming/default"}],
 "isAdminCreds":false,"uuid":"7f48e8e8-5e89-4220-b064-fa62cbd0ff2f",
 "implementationVersion":"1.8.0r-55-g80f24f2-enterprise",
 "componentsVersion":{"os_mon":"2.2.6","mnesia":"4.4.19","inets":"5.6","kernel":"2.14.4",
                      "sasl":"2.1.9.4","ns_server":"1.8.0r-55-g80f24f2-enterprise",
                      "stdlib":"1.17.4"}}
"""

SAMPLE_JSON_pools_default = """
{"storageTotals":{
  "ram":{"quotaUsed":629145600,"usedByData":54117632,"total":8312143872.0,
         "quotaTotal":6647971840.0,"used":4401639424.0},
  "hdd":{"usedByData":5117960,"total":26966704128.0,
         "quotaTotal":26966704128.0,"used":5258507304.0,"free":21573363304.0}},
  "name":"default","alerts":[],
  "nodes":[
    {"systemStats":{
       "cpu_utilization_rate":0.2631578947368421,
       "swap_total":1073737728,"swap_used":0},
     "interestingStats":{"curr_items":0,"curr_items_tot":0,"vb_replica_curr_items":0},
     "uptime":"745","memoryTotal":4156071936.0,"memoryFree":1760247808,
     "mcdMemoryReserved":3170,"mcdMemoryAllocated":3170,
     "clusterMembership":"active",
     "status":"healthy",
     "hostname":"HOST0:8091",
     "clusterCompatibility":1,
     "version":"1.8.0r-55-g80f24f2-enterprise",
     "os":"x86_64-unknown-linux-gnu",
     "ports":{"proxy":11211,"direct":11210}},
    {"systemStats":{
       "cpu_utilization_rate":0.7389162561576355,
       "swap_total":1073737728,"swap_used":0},
     "interestingStats":{"curr_items":0,"curr_items_tot":0,"vb_replica_curr_items":0},
     "uptime":"735","memoryTotal":4156071936.0,"memoryFree":2150256640.0,
     "mcdMemoryReserved":3170,"mcdMemoryAllocated":3170,
     "clusterMembership":"active",
     "status":"healthy",
     "hostname":"HOST1:8091",
     "clusterCompatibility":1,
     "version":"1.8.0r-55-g80f24f2-enterprise",
     "os":"x86_64-unknown-linux-gnu",
     "ports":{"proxy":11211,"direct":11210}}
  ],
  "buckets":{
    "uri":"/pools/default/buckets?v=2979176"},
    "controllers":{
      "addNode":{"uri":"/controller/addNode"},
      "rebalance":{"uri":"/controller/rebalance"},
      "failOver":{"uri":"/controller/failOver"},
      "reAddNode":{"uri":"/controller/reAddNode"},
      "ejectNode":{"uri":"/controller/ejectNode"},
      "testWorkload":{"uri":"/pools/default/controller/testWorkload"}},
    "balanced":true,
    "failoverWarnings":[],
    "rebalanceStatus":"none",
    "rebalanceProgressUri":"/pools/default/rebalanceProgress",
    "stopRebalanceUri":"/controller/stopRebalance",
    "nodeStatusesUri":"/nodeStatuses",
    "stats":{"uri":"/pools/default/stats"},
    "counters":{"rebalance_success":1,"rebalance_start":1}}
"""

SAMPLE_JSON_pools_default_buckets = """
[{"name":"default","bucketType":"membase",
  "authType":"sasl","saslPassword":"",
  "proxyPort":0,
  "uri":"/pools/default/buckets/default",
  "streamingUri":"/pools/default/bucketsStreaming/default",
  "flushCacheUri":"/pools/default/buckets/default/controller/doFlush",
  "nodes":[
    {"systemStats":{"cpu_utilization_rate":0.25,"swap_total":1073737728,"swap_used":0},
     "interestingStats":{"curr_items":0,"curr_items_tot":0,"vb_replica_curr_items":0},
     "uptime":"1210","memoryTotal":4156071936.0,"memoryFree":1757093888,
     "mcdMemoryReserved":3170,"mcdMemoryAllocated":3170,
     "replication":1.0,
     "clusterMembership":"active",
     "status":"healthy",
     "hostname":"HOST0:8091",
     "clusterCompatibility":1,
     "version":"1.8.0r-55-g80f24f2-enterprise",
     "os":"x86_64-unknown-linux-gnu",
     "CAPIk0":"CAPIv0",
     "ports":{"proxy":11211,"direct":11210}},
    {"systemStats":{"cpu_utilization_rate":0.49875311720698257,"swap_total":1073737728,"swap_used":0},
     "interestingStats":{"curr_items":0,"curr_items_tot":0,"vb_replica_curr_items":0},
     "uptime":"1205","memoryTotal":4156071936.0,"memoryFree":2142023680,
     "mcdMemoryReserved":3170,"mcdMemoryAllocated":3170,
     "replication":1.0,
     "clusterMembership":"active",
     "status":"healthy",
     "hostname":"HOST1:8091",
     "clusterCompatibility":1,
     "version":"1.8.0r-55-g80f24f2-enterprise",
     "os":"x86_64-unknown-linux-gnu",
     "CAPIk1":"CAPIv1",
     "ports":{"proxy":11211,"direct":11210}}],
   "stats":{"uri":"/pools/default/buckets/default/stats",
            "directoryURI":"/pools/default/buckets/default/statsDirectory",
            "nodeStatsListURI":"/pools/default/buckets/default/nodes"},
   "nodeLocator":"vbucket",
   "vBucketServerMap":{
     "hashAlgorithm":"CRC",
     "numReplicas":1,
     "serverList":["HOST0:11210","HOST1:11210"],
     "vBucketMap":[[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[0,1],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0],[1,0]]},
   "replicaNumber":1,
   "quota":{"ram":629145600,"rawRAM":314572800},
   "basicStats":{"quotaPercentUsed":8.601765950520834,"opsPerSec":0,"diskFetches":0,
                 "itemCount":0,"diskUsed":5117960,"memUsed":54117632}}]
"""

class MockStdOut:
    def __init__(self):
        self.msgs = []
        self.items = []

    def write(self, m):
        self.msgs.append(str(m))

    def item_visitor(self, item):
        self.items.append(item)
        return item


if __name__ == '__main__':
    unittest.main()

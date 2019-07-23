#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  Project                     ___| | | |  _ \| |
#                             / __| | | | |_) | |
#                            | (__| |_| |  _ <| |___
#                             \___|\___/|_| \_\_____|
#
# Copyright (C) 2017, Daniel Stenberg, <daniel@haxx.se>, et al.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at https://curl.haxx.se/docs/copyright.html.
#
# You may opt to use, copy, modify, merge, publish, distribute and/or sell
# copies of the Software, and permit persons to whom the Software is
# furnished to do so, under the terms of the COPYING file.
#
# This software is distributed on an "AS IS" basis, WITHOUT WARRANTY OF ANY
# KIND, either express or implied.
#
"""Server for testing SMB"""

from __future__ import (absolute_import, division, print_function)
# unicode_literals)
import argparse
import ConfigParser
import os
import sys
import logging
import tempfile

# Import our curl test data helper
import curl_test_data

# This saves us having to set up the PYTHONPATH explicitly
deps_dir = os.path.join(os.path.dirname(__file__), "python_dependencies")
sys.path.append(deps_dir)
from impacket import smbserver as imp_smbserver
from impacket import smb as imp_smb
from impacket.nt_errors import (STATUS_ACCESS_DENIED, STATUS_SUCCESS,
                                STATUS_NO_SUCH_FILE)

log = logging.getLogger(__name__)
SERVER_MAGIC = "SERVER_MAGIC"
TESTS_MAGIC = "TESTS_MAGIC"
VERIFIED_REQ = "verifiedserver"
VERIFIED_RSP = b"WE ROOLZ: {pid}\n"


def smbserver(options):
    """Start up a TCP SMB server that serves forever

    """
    if options.pidfile:
        pid = os.getpid()
        with open(options.pidfile, "w") as f:
            f.write("{0}".format(pid))

    # Here we write a mini config for the server
    smb_config = ConfigParser.ConfigParser()
    smb_config.add_section("global")
    smb_config.set("global", "server_name", "SERVICE")
    smb_config.set("global", "server_os", "UNIX")
    smb_config.set("global", "server_domain", "WORKGROUP")
    smb_config.set("global", "log_file", "")
    smb_config.set("global", "credentials_file", "")

    # We need a share which allows us to test that the server is running
    smb_config.add_section("SERVER")
    smb_config.set("SERVER", "comment", "server function")
    smb_config.set("SERVER", "read only", "yes")
    smb_config.set("SERVER", "share type", "0")
    smb_config.set("SERVER", "path", SERVER_MAGIC)

    # Have a share for tests.  These files will be autogenerated from the
    # test input.
    smb_config.add_section("TESTS")
    smb_config.set("TESTS", "comment", "tests")
    smb_config.set("TESTS", "read only", "yes")
    smb_config.set("TESTS", "share type", "0")
    smb_config.set("TESTS", "path", TESTS_MAGIC)

    if not options.srcdir or not os.path.isdir(options.srcdir):
        raise ScriptException("--srcdir is mandatory")

    test_data_dir = os.path.join(options.srcdir, "data")

    smb_server = TestSmbServer((options.host, options.port),
                               config_parser=smb_config,
                               test_data_directory=test_data_dir)
    log.info("[SMB] setting up SMB server on port %s", options.port)
    smb_server.processConfigFile()
    smb_server.serve_forever()
    return 0


class TestSmbServer(imp_smbserver.SMBSERVER):
    """
    Test server for SMB which subclasses the impacket SMBSERVER and provides
    test functionality.
    """

    def __init__(self,
                 address,
                 config_parser=None,
                 test_data_directory=None):
        imp_smbserver.SMBSERVER.__init__(self,
                                         address,
                                         config_parser=config_parser)

        # Set up a test data object so we can get test data later.
        self.ctd = curl_test_data.TestData(test_data_directory)

        # Override smbComNtCreateAndX so we can pretend to have files which
        # don't exist.
        self.hookSmbCommand(imp_smb.SMB.SMB_COM_NT_CREATE_ANDX,
                            self.create_and_x)

    def create_and_x(self, conn_id, smb_server, smb_command, recv_packet):
        """
        Our version of smbComNtCreateAndX looks for special test files and
        fools the rest of the framework into opening them as if they were
        normal files.
        """
        conn_data = smb_server.getConnectionData(conn_id)

        # Wrap processing in a try block which allows us to throw SmbException
        # to control the flow.
        try:
            ncax_parms = imp_smb.SMBNtCreateAndX_Parameters(
                smb_command["Parameters"])

            path = self.get_share_path(conn_data,
                                       ncax_parms["RootFid"],
                                       recv_packet["Tid"])
            log.info("[SMB] Requested share path: %s", path)

            disposition = ncax_parms["Disposition"]
            log.debug("[SMB] Requested disposition: %s", disposition)

            # Currently we only support reading files.
            if disposition != imp_smb.FILE_OPEN:
                raise SmbException(STATUS_ACCESS_DENIED,
                                   "Only support reading files")

            # Check to see if the path we were given is actually a
            # magic path which needs generating on the fly.
            if path not in [SERVER_MAGIC, TESTS_MAGIC]:
                # Pass the command onto the original handler.
                return imp_smbserver.SMBCommands.smbComNtCreateAndX(conn_id,
                                                                    smb_server,
                                                                    smb_command,
                                                                    recv_packet)

            flags2 = recv_packet["Flags2"]
            ncax_data = imp_smb.SMBNtCreateAndX_Data(flags=flags2,
                                                     data=smb_command[
                                                         "Data"])
            requested_file = imp_smbserver.decodeSMBString(
                flags2,
                ncax_data["FileName"])
            log.debug("[SMB] User requested file '%s'", requested_file)

            if path == SERVER_MAGIC:
                fid, full_path = self.get_server_path(requested_file)
            else:
                assert (path == TESTS_MAGIC)
                fid, full_path = self.get_test_path(requested_file)

            resp_parms = imp_smb.SMBNtCreateAndXResponse_Parameters()
            resp_data = ""

            # Simple way to generate a fid
            if len(conn_data["OpenedFiles"]) == 0:
                fakefid = 1
            else:
                fakefid = conn_data["OpenedFiles"].keys()[-1] + 1
            resp_parms["Fid"] = fakefid
            resp_parms["CreateAction"] = disposition

            if os.path.isdir(path):
                resp_parms[
                    "FileAttributes"] = imp_smb.SMB_FILE_ATTRIBUTE_DIRECTORY
                resp_parms["IsDirectory"] = 1
            else:
                resp_parms["IsDirectory"] = 0
                resp_parms["FileAttributes"] = ncax_parms["FileAttributes"]

            # Get this file's information
            resp_info, error_code = imp_smbserver.queryPathInformation(
                "", full_path, level=imp_smb.SMB_QUERY_FILE_ALL_INFO)

            if error_code != STATUS_SUCCESS:
                raise SmbException(error_code, "Failed to query path info")

            resp_parms["CreateTime"] = resp_info["CreationTime"]
            resp_parms["LastAccessTime"] = resp_info[
                "LastAccessTime"]
            resp_parms["LastWriteTime"] = resp_info["LastWriteTime"]
            resp_parms["LastChangeTime"] = resp_info[
                "LastChangeTime"]
            resp_parms["FileAttributes"] = resp_info[
                "ExtFileAttributes"]
            resp_parms["AllocationSize"] = resp_info[
                "AllocationSize"]
            resp_parms["EndOfFile"] = resp_info["EndOfFile"]

            # Let's store the fid for the connection
            # smbServer.log("Create file %s, mode:0x%x" % (pathName, mode))
            conn_data["OpenedFiles"][fakefid] = {}
            conn_data["OpenedFiles"][fakefid]["FileHandle"] = fid
            conn_data["OpenedFiles"][fakefid]["FileName"] = path
            conn_data["OpenedFiles"][fakefid]["DeleteOnClose"] = False

        except SmbException as s:
            log.debug("[SMB] SmbException hit: %s", s)
            error_code = s.error_code
            resp_parms = ""
            resp_data = ""

        resp_cmd = imp_smb.SMBCommand(imp_smb.SMB.SMB_COM_NT_CREATE_ANDX)
        resp_cmd["Parameters"] = resp_parms
        resp_cmd["Data"] = resp_data
        smb_server.setConnectionData(conn_id, conn_data)

        return [resp_cmd], None, error_code

    def get_share_path(self, conn_data, root_fid, tid):
        conn_shares = conn_data["ConnectedShares"]

        if tid in conn_shares:
            if root_fid > 0:
                # If we have a rootFid, the path is relative to that fid
                path = conn_data["OpenedFiles"][root_fid]["FileName"]
                log.debug("RootFid present %s!" % path)
            else:
                if "path" in conn_shares[tid]:
                    path = conn_shares[tid]["path"]
                else:
                    raise SmbException(STATUS_ACCESS_DENIED,
                                       "Connection share had no path")
        else:
            raise SmbException(imp_smbserver.STATUS_SMB_BAD_TID,
                               "TID was invalid")

        return path

    def get_server_path(self, requested_filename):
        log.debug("[SMB] Get server path '%s'", requested_filename)

        if requested_filename not in [VERIFIED_REQ]:
            raise SmbException(STATUS_NO_SUCH_FILE, "Couldn't find the file")

        fid, filename = tempfile.mkstemp()
        log.debug("[SMB] Created %s (%d) for storing '%s'",
                  filename, fid, requested_filename)

        contents = ""

        if requested_filename == VERIFIED_REQ:
            log.debug("[SMB] Verifying server is alive")
            contents = VERIFIED_RSP.format(pid=os.getpid())

        self.write_to_fid(fid, contents)
        return fid, filename

    def write_to_fid(self, fid, contents):
        # Write the contents to file descriptor
        os.write(fid, contents)
        os.fsync(fid)

        # Rewind the file to the beginning so a read gets us the contents
        os.lseek(fid, 0, os.SEEK_SET)

    def get_test_path(self, requested_filename):
        log.info("[SMB] Get reply data from 'test%s'", requested_filename)

        fid, filename = tempfile.mkstemp()
        log.debug("[SMB] Created %s (%d) for storing test '%s'",
                  filename, fid, requested_filename)

        try:
            contents = self.ctd.get_test_data(requested_filename)
            self.write_to_fid(fid, contents)
            return fid, filename

        except Exception:
            log.exception("Failed to make test file")
            raise SmbException(STATUS_NO_SUCH_FILE, "Failed to make test file")


class SmbException(Exception):
    def __init__(self, error_code, error_message):
        super(SmbException, self).__init__(error_message)
        self.error_code = error_code


class ScriptRC(object):
    """Enum for script return codes"""
    SUCCESS = 0
    FAILURE = 1
    EXCEPTION = 2


class ScriptException(Exception):
    pass


def get_options():
    parser = argparse.ArgumentParser()

    parser.add_argument("--port", action="store", default=9017,
                      type=int, help="port to listen on")
    parser.add_argument("--host", action="store", default="127.0.0.1",
                      help="host to listen on")
    parser.add_argument("--verbose", action="store", type=int, default=0,
                        help="verbose output")
    parser.add_argument("--pidfile", action="store",
                        help="file name for the PID")
    parser.add_argument("--logfile", action="store",
                        help="file name for the log")
    parser.add_argument("--srcdir", action="store", help="test directory")
    parser.add_argument("--id", action="store", help="server ID")
    parser.add_argument("--ipv4", action="store_true", default=0,
                        help="IPv4 flag")

    return parser.parse_args()


def setup_logging(options):
    """
    Set up logging from the command line options
    """
    root_logger = logging.getLogger()
    add_stdout = False

    formatter = logging.Formatter("%(asctime)s %(levelname)-5.5s %(message)s")

    # Write out to a logfile
    if options.logfile:
        handler = logging.FileHandler(options.logfile, mode="w")
        handler.setFormatter(formatter)
        handler.setLevel(logging.DEBUG)
        root_logger.addHandler(handler)
    else:
        # The logfile wasn't specified. Add a stdout logger.
        add_stdout = True

    if options.verbose:
        # Add a stdout logger as well in verbose mode
        root_logger.setLevel(logging.DEBUG)
        add_stdout = True
    else:
        root_logger.setLevel(logging.INFO)

    if add_stdout:
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.setLevel(logging.DEBUG)
        root_logger.addHandler(stdout_handler)


if __name__ == '__main__':
    # Get the options from the user.
    options = get_options()

    # Setup logging using the user options
    setup_logging(options)

    # Run main script.
    try:
        rc = smbserver(options)
    except Exception as e:
        log.exception(e)
        rc = ScriptRC.EXCEPTION

    log.info("[SMB] Returning %d", rc)
    sys.exit(rc)

"""
Objects for dealing with Qiita analyses

This module provides the implementation of the Analysis class.

Classes
-------
- `Analysis` -- A Qiita Analysis class
"""

# -----------------------------------------------------------------------------
# Copyright (c) 2014--, The Qiita Development Team.
#
# Distributed under the terms of the BSD 3-clause License.
#
# The full license is in the file LICENSE, distributed with this software.
# -----------------------------------------------------------------------------
from __future__ import division
from collections import defaultdict
from os.path import join, basename, splitext, relpath
from binascii import crc32

from future.builtins import zip
from future.utils import viewitems
from biom import load_table
from biom.table import Table
from biom.util import biom_open

from qiita_core.exceptions import IncompetentQiitaDeveloperError
from .sql_connection import SQLConnectionHandler
from .base import QiitaStatusObject
from .data import ProcessedData
from .exceptions import QiitaDBNotImplementedError, QiitaDBStatusError
from .util import (convert_to_id, get_work_base_dir, get_db_files_base_dir,
                   get_table_cols)


class Analysis(QiitaStatusObject):
    """
    Analysis object to access to the Qiita Analysis information

    Attributes
    ----------
    owner
    name
    description
    samples
    data_types
    biom_tables
    step
    shared_with
    jobs
    pmid
    parent
    children

    Methods
    -------
    add_samples
    remove_samples
    share
    unshare
    finalize_analysis
    """

    _table = "analysis"

    def _lock_check(self, conn_handler):
        """Raises QiitaDBStatusError if analysis is not in_progress"""
        if self.check_status({"public", "completed", "error", "running",
                              "queued"}):
            raise QiitaDBStatusError("Analysis is locked!")

    def _status_setter_checks(self, conn_handler):
        r"""Perform a check to make sure not setting status away from public
        """
        self._lock_check(conn_handler)

    @classmethod
    def get_public(cls):
        """Returns analysis id for all public Analyses

        Returns
        -------
        list of int
            All public analysses in the database
        """
        conn_handler = SQLConnectionHandler()
        sql = ("SELECT analysis_id FROM qiita.{0} WHERE "
               "{0}_status_id = %s".format(cls._table))
        # MAGIC NUMBER 6: status id for a public study
        return [x[0] for x in conn_handler.execute_fetchall(sql, (6,))]

    @classmethod
    def create(cls, owner, name, description, parent=None):
        """Creates a new analysis on the database

        Parameters
        ----------
        owner : User object
            The analysis' owner
        name : str
            Name of the analysis
        description : str
            Description of the analysis
        parent : Analysis object, optional
            The analysis this one was forked from
        """
        conn_handler = SQLConnectionHandler()
        # TODO after demo: if exists()

        # insert analysis information into table with "in construction" status
        sql = ("INSERT INTO qiita.{0} (email, name, description, "
               "analysis_status_id) VALUES (%s, %s, %s, 1) "
               "RETURNING analysis_id".format(cls._table))
        a_id = conn_handler.execute_fetchone(
            sql, (owner.id, name, description))[0]

        # add parent if necessary
        if parent:
            sql = ("INSERT INTO qiita.analysis_chain (parent_id, child_id) "
                   "VALUES (%s, %s)")
            conn_handler.execute(sql, (parent.id, a_id))

        return cls(a_id)

    # ---- Properties ----
    @property
    def owner(self):
        """The owner of the analysis

        Returns
        -------
        str
            Name of the Analysis
        """
        conn_handler = SQLConnectionHandler()
        sql = ("SELECT email FROM qiita.{0} WHERE "
               "analysis_id = %s".format(self._table))
        return conn_handler.execute_fetchone(sql, (self._id, ))[0]

    @property
    def name(self):
        """The name of the analysis

        Returns
        -------
        str
            Name of the Analysis
        """
        conn_handler = SQLConnectionHandler()
        sql = ("SELECT name FROM qiita.{0} WHERE "
               "analysis_id = %s".format(self._table))
        return conn_handler.execute_fetchone(sql, (self._id, ))[0]

    @property
    def description(self):
        """Returns the description of the analysis"""
        conn_handler = SQLConnectionHandler()
        sql = ("SELECT description FROM qiita.{0} WHERE "
               "analysis_id = %s".format(self._table))
        return conn_handler.execute_fetchone(sql, (self._id, ))[0]

    @description.setter
    def description(self, description):
        """Changes the description of the analysis

        Parameters
        ----------
        description : str
            New description for the analysis

        Raises
        ------
        QiitaDBStatusError
            Analysis is public
        """
        conn_handler = SQLConnectionHandler()
        self._lock_check(conn_handler)
        sql = ("UPDATE qiita.{0} SET description = %s WHERE "
               "analysis_id = %s".format(self._table))
        conn_handler.execute(sql, (description, self._id))

    @property
    def samples(self):
        """The processed data and samples attached to the analysis

        Returns
        -------
        dict
            Format is {processed_data_id: [sample_id, sample_id, ...]}
        """
        conn_handler = SQLConnectionHandler()
        sql = ("SELECT processed_data_id, sample_id FROM qiita.analysis_sample"
               " WHERE analysis_id = %s ORDER BY processed_data_id")
        ret_samples = defaultdict(list)
        # turn into dict of samples keyed to processed_data_id
        for pid, sample in conn_handler.execute_fetchall(sql, (self._id, )):
            ret_samples[pid].append(sample)
        return ret_samples

    @property
    def data_types(self):
        """Returns all data types used in the analysis

        Returns
        -------
        list of str
            Data types in the analysis
        """
        sql = ("SELECT DISTINCT data_type from qiita.data_type d JOIN "
               "qiita.processed_data p ON p.data_type_id = d.data_type_id "
               "JOIN qiita.analysis_sample a ON p.processed_data_id = "
               "a.processed_data_id WHERE a.analysis_id = %s ORDER BY "
               "data_type")
        conn_handler = SQLConnectionHandler()
        return [x[0] for x in conn_handler.execute_fetchall(sql, (self._id, ))]

    @property
    def shared_with(self):
        """The user the analysis is shared with

        Returns
        -------
        list of int
            User ids analysis is shared with
        """
        conn_handler = SQLConnectionHandler()
        sql = ("SELECT email FROM qiita.analysis_users WHERE "
               "analysis_id = %s")
        return [u[0] for u in conn_handler.execute_fetchall(sql, (self._id, ))]

    @property
    def biom_tables(self):
        """The biom tables of the analysis

        Returns
        -------
        dict or None
            Dictonary in the form {data_type: full BIOM filepath} or None if
            not generated
        """
        conn_handler = SQLConnectionHandler()
        fptypeid = convert_to_id("biom", "filepath_type")
        sql = ("SELECT af.data_type, f.filepath FROM qiita.filepath f JOIN "
               "qiita.analysis_filepath af ON f.filepath_id = af.filepath_id "
               "WHERE af.analysis_id = %s AND f.filepath_type_id = %s")
        tables = conn_handler.execute_fetchall(sql, (self._id, fptypeid))
        if not tables:
            return None
        ret_tables = {}
        base_fp = get_db_files_base_dir()
        for fp in tables:
            ret_tables[fp[0]] = join(base_fp, "analysis", fp[1])
        return ret_tables

    @property
    def mapping_file(self):
        """Returns the mapping file for the analysis

        Returns
        -------
        str or None
            full filepath to the mapping file or None if not generated
        """
        conn_handler = SQLConnectionHandler()
        fptypeid = convert_to_id("plain_text", "filepath_type")
        sql = ("SELECT f.filepath FROM qiita.filepath f JOIN "
               "qiita.analysis_filepath af ON f.filepath_id = af.filepath_id "
               "WHERE af.analysis_id = %s AND f.filepath_type_id = %s")
        mapping_fp = conn_handler.execute_fetchone(sql, (self._id, fptypeid))
        if not mapping_fp:
            return None
        return join(get_db_files_base_dir(), "analysis", mapping_fp)

    @property
    def step(self):
        conn_handler = SQLConnectionHandler()
        self._lock_check(conn_handler)
        sql = "SELECT step from qiita.analysis_workflow WHERE analysis_id = %s"
        try:
            return conn_handler.execute_fetchone(sql, (self._id,))[0]
        except TypeError:
            raise ValueError("Step not set yet!")

    @step.setter
    def step(self, value):
        conn_handler = SQLConnectionHandler()
        self._lock_check(conn_handler)
        sql = ("SELECT EXISTS(SELECT analysis_id from qiita.analysis_workflow "
               "WHERE analysis_id = %s)")
        step_exists = conn_handler.execute_fetchone(sql, (self._id,))[0]

        if step_exists:
            sql = ("UPDATE qiita.analysis_workflow SET step = %s WHERE "
                   "analysis_id = %s")
        else:
            sql = ("INSERT INTO qiita.analysis_workflow (step, analysis_id) "
                   "VALUES (%s, %s)")
        conn_handler.execute(sql, (value, self._id))

    @property
    def jobs(self):
        """A list of jobs included in the analysis

        Returns
        -------
        list of ints
            Job ids for jobs in analysis
        """
        conn_handler = SQLConnectionHandler()
        sql = ("SELECT job_id FROM qiita.analysis_job WHERE "
               "analysis_id = %s".format(self._table))
        job_ids = conn_handler.execute_fetchall(sql, (self._id, ))
        if job_ids == []:
            return None
        return [job_id[0] for job_id in job_ids]

    @property
    def pmid(self):
        """Returns pmid attached to the analysis

        Returns
        -------
        str or None
            returns the PMID or None if none is attached
        """
        conn_handler = SQLConnectionHandler()
        sql = ("SELECT pmid FROM qiita.{0} WHERE "
               "analysis_id = %s".format(self._table))
        pmid = conn_handler.execute_fetchone(sql, (self._id, ))[0]
        return pmid

    @pmid.setter
    def pmid(self, pmid):
        """adds pmid to the analysis

        Parameters
        ----------
        pmid: str
            pmid to set for study

        Raises
        ------
        QiitaDBStatusError
            Analysis is public

        Notes
        -----
        An analysis should only ever have one PMID attached to it.
        """
        conn_handler = SQLConnectionHandler()
        self._lock_check(conn_handler)
        sql = ("UPDATE qiita.{0} SET pmid = %s WHERE "
               "analysis_id = %s".format(self._table))
        conn_handler.execute(sql, (pmid, self._id))

    # @property
    # def parent(self):
    #     """Returns the id of the parent analysis this was forked from"""
    #     return QiitaDBNotImplementedError()

    # @property
    # def children(self):
    #     return QiitaDBNotImplementedError()

    # ---- Functions ----
    def share(self, user):
        """Share the analysis with another user

        Parameters
        ----------
        user: User object
            The user to share the study with
        """
        conn_handler = SQLConnectionHandler()
        self._lock_check(conn_handler)

        sql = ("INSERT INTO qiita.analysis_users (analysis_id, email) VALUES "
               "(%s, %s)")
        conn_handler.execute(sql, (self._id, user.id))

    def unshare(self, user):
        """Unshare the analysis with another user

        Parameters
        ----------
        user: User object
            The user to unshare the study with
        """
        conn_handler = SQLConnectionHandler()
        self._lock_check(conn_handler)

        sql = ("DELETE FROM qiita.analysis_users WHERE analysis_id = %s AND "
               "email = %s")
        conn_handler.execute(sql, (self._id, user.id))

    def add_samples(self, samples):
        """Adds samples to the analysis

        Parameters
        ----------
        samples : list of tuples
            samples and the processed data id they come from in form
            [(processed_data_id, sample_id), ...]
        """
        conn_handler = SQLConnectionHandler()
        self._lock_check(conn_handler)
        sql = ("INSERT INTO qiita.analysis_sample (analysis_id, sample_id, "
               "processed_data_id) VALUES (%s, %s, %s)")
        conn_handler.executemany(sql, [(self._id, s[1], s[0])
                                       for s in samples])

    def remove_samples(self, proc_data=None, samples=None):
        """Removes samples from the analysis

        Parameters
        ----------
        proc_data : list, optional
            processed data ids to remove, default None
        samples : list, optional
            sample ids to remove, default None

        Notes
        -----
        When only a list of samples given, the samples will be removed from all
        processed data ids it is associated with

        When only a list of proc_data given, all samples associated with that
        processed data are removed

        If both are passed, the given samples are removed from the given
        processed data ids
        """
        conn_handler = SQLConnectionHandler()
        self._lock_check(conn_handler)
        if proc_data and samples:
            sql = ("DELETE FROM qiita.analysis_sample WHERE analysis_id = %s "
                   "AND processed_data_id = %s AND sample_id = %s")
            remove = []
            # build tuples for what samples to remove from what processed data
            for proc_id in proc_data:
                for sample_id in samples:
                    remove.append((self._id, proc_id, sample_id))
        elif proc_data:
            sql = ("DELETE FROM qiita.analysis_sample WHERE analysis_id = %s "
                   "AND processed_data_id = %s")
            remove = [(self._id, p) for p in proc_data]
        elif samples:
            sql = ("DELETE FROM qiita.analysis_sample WHERE analysis_id = %s "
                   "AND sample_id = %s")
            remove = [(self._id, s) for s in samples]
        else:
            raise IncompetentQiitaDeveloperError(
                "Must provide list of samples and/or proc_data for removal!")

        conn_handler.executemany(sql, remove)

    def finalize_analysis(self):
        """Does all final setup needed for analysis to run

        Notes
        -----
        Creates biom tables for each requested data type
        Creates mapping file for requested samples
        Sets analysis status to queued
        """
        conn_handler = SQLConnectionHandler()
        self._build_biom_tables(conn_handler)
        self._build_mapping_file(conn_handler)
        self.status = 'queued'

    def _get_samples(self, conn_handler=None):
        """Retrieves dict of samples to proc_data_id for the analysis"""
        conn_handler = conn_handler if conn_handler is not None \
            else SQLConnectionHandler()
        sql = ("SELECT processed_data_id, array_agg(sample_id ORDER BY "
               "sample_id) FROM qiita.analysis_sample WHERE analysis_id = %s "
               "GROUP BY processed_data_id")
        return dict(conn_handler.execute_fetchall(sql, [self._id]))

    def _build_biom_tables(self, conn_handler=None):
        """Build tables and add them to the analysis"""
        conn_handler = conn_handler if conn_handler is not None \
            else SQLConnectionHandler()
        samples = _get_samples(conn_handler)
        # filter and combine all study BIOM tables needed for each data type
        new_tables = {dt: None for dt in self.data_types}
        base_fp = get_work_base_dir()
        for pid, samps in viewitems(samples):
            # one biom table attached to each processed data object
            proc_data = ProcessedData(pid)
            proc_data_fp = proc_data.get_filepaths()[0][0]
            table_fp = join(base_fp, proc_data_fp)
            table = load_table(table_fp)
            # filter for just the wanted samples and merge into new table
            # this if/else setup avoids needing a blank table to start merges
            table.filter(samps, axis='sample', inplace=True)
            data_type = proc_data.data_type
            if new_tables[data_type] is None:
                new_tables[data_type] = table
            else:
                new_tables[data_type].merge(table)

        # add the new tables to the analysis
        base_fp = get_db_files_base_dir()
        for dt, biom_table in viewitems(new_tables):
            # write out the file
            biom_fp = join(base_fp, "analysis", "%d_analysis_%s.biom" %
                           (self._id, dt))
            self._add_file("%d_analysis_%s.biom" % (self._id, dt),
                           "biom", data_type=dt, conn_handler=conn_handler)

    def _build_mapping_file(self, conn_handler=None):
        """Builds the combined mapping file for all samples
           Code modified slightly from qiime.util.MetadataMap.__add__"""
        conn_handler = conn_handler if conn_handler is not None \
            else SQLConnectionHandler()
        # We will keep track of all unique sample_ids and metadata headers
        # we have seen as we go, as well as studies already seen
        all_sample_ids = set()
        all_headers = set()
        all_studies = set()

        samples = self._get_samples(conn_handler)
        merged_data = defaultdict(lambda: defaultdict(lambda: None))
        for pid, samples in viewitems(samples):
            study = ProcessedData(pid).study
            if study in all_studies:
                # samples already added by other processed data file in study
                continue
            all_studies.add(study)
            # query out the combined table of metadata for all samples
            headers = get_table_cols("sample_%d" % study, conn_handler)
            headers.remove("sample_id")
            sql = ("SELECT rs.sample_type, rs.collection_timestamp, "
                   "rs.host_subject_id,rs.description,{0},rs.sample_id "
                   "FROM qiita.required_sample_info rs JOIN qiita.sample_{1} "
                   "ss USING(sample_id) WHERE rs.sample_id IN {2} AND "
                   "rs.study_id = {1}".format(
                       ",".join("ss.%s" % h for h in headers),
                       study,
                       "(%s)" % ",".join("'%s'" % s for s in samples)))
            metadata = conn_handler.execute_fetchall(sql)
            headers = ["sample_type", "collection_timestamp",
                       "host_subject_id", "description"] + headers
            all_headers.update(headers)
            # add all the metadata to merged_data
            for data in metadata:
                sample_id = data.pop()
                if sample_id not in all_sample_ids:
                    all_sample_ids.add(sample_id)
                else:
                    raise ValueError("Duplicate sample id found: %s" %
                                     sample_id)

                for header, value in zip(headers, data):
                    merged_data[sample_id][header] = str(value)

        # prep headers, making sure they follow mapping file format rules
        all_headers.remove('description')
        all_headers = list(all_headers)
        all_headers.sort()
        all_headers.append('description')

        # write mapping file out
        base_fp = get_db_files_base_dir()
        mapping_fp = join(base_fp, "analysis", "%d_analysis_mapping.txt" %
                          self._id)
        with open(mapping_fp, 'w') as f:
            f.write("#SampleID\t%s\n" % '\t'.join(all_headers))
            for sample, metadata in viewitems(merged_data):
                data = [sample]
                for header in all_headers:
                    data.append(metadata[header] if
                                metadata[header] is not None else "no_data")
                f.write("%s\n" % "\t".join(data))

        self._add_file("%d_analysis_mapping.txt" % self._id,
                       "plain_text", conn_handler=conn_handler)

    def _add_file(self, filename, filetype, data_type=None, conn_handler=None):
        """adds analysis item to database

        Parameters
        ----------
        filename : str
            filename to add to analysis
        filetype : {plain_text, biom}
        data_type : str, optional
        conn_handler : SQLConnectionHandler object, optional
        """
        conn_handler = conn_handler if conn_handler is not None \
            else SQLConnectionHandler()

        # get required bookkeeping data for DB
        with open(filename, 'rb') as f:
            checksum = crc32(f.read()) & 0xffffffff
        fptypeid = convert_to_id(filetype, "filepath_type", conn_handler)

        # add  file to analysis
        sql = ("INSERT INTO qiita.filepath (filepath, filepath_type_id, "
               "checksum, checksum_algorithm_id) VALUES (%s, %s, %s, %s) "
               "RETURNING filepath_id")
        # magic number 1 is for crc32 checksum algorithm
        fpid = conn_handler.execute_fetchone(sql, (filename, fptypeid,
                                                   checksum, 1))

        dtinfo = ["", ""]
        if datatype:
            dtinfo[0] = ",data_type_id"
            dtinfo[1] = ",%d" % convert_to_id(data_type, "data_type")

        sql = ("INSERT INTO qiita.analysis_filepath (analysis_id, filepath_id"
               "{0}) VALUES (%s, %s{1})".format(**dtinfo))
        conn_handler.execute(sql, (self._id, fpid))

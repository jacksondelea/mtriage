import glob
import os
import shutil
import traceback
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Generator, List, Union, Tuple
from lib.common.exceptions import (
    ElementShouldSkipError,
    ElementShouldRetryError,
    InvalidAnalyserConfigError,
    MTriageStorageCorruptedError,
    InvalidElementsIn,
    InvalidAnalyserElements,
)
from lib.common.mtmodule import MTModule
from lib.common.storage import Storage
from lib.common.etypes import LocalElement


class Analyser(MTModule):
    """A Analyser is a pass that creates derived workables from retrieved data.

    The working directory of the selector is passed during class instantiation, and can be referenced in the
    implementations of methods.
    """

    errored = False

    def __init__(self, config, module, storage=None):
        super().__init__(config, module, storage)

        if not isinstance(module, str) or module == "":
            raise InvalidAnalyserConfigError(
                "You must provide a name for your analyser"
            )
        if not isinstance(storage, Storage):
            raise InvalidAnalyserConfigError("You must provide a valid storage object")
        if not "elements_in" in config:
            raise InvalidAnalyserConfigError(
                "The config must contain an 'elements_in' indicating the analyser's input."
            )
        if not config["elements_in"] or not isinstance(config["elements_in"], list):
            raise InvalidAnalyserConfigError(
                "The 'elements_in' must be a list containing at least one string"
            )

    @abstractmethod
    def analyse_element(
        self, element: LocalElement, config
    ) -> Union[LocalElement, None]:
        """Method defined on each analyser that implements analysis element-wise.

        An element is currently simply a path to the relevant media. TODO: elements should be a more structured
        type.

        Should create a new element in the appropriate element[]'base' dir.
        """
        return NotImplemented

    def pre_analyse(self, config):
        """option to set up class variables"""

    def post_analyse(self, config):
        """option to perform any clear up"""
        return None

    def start_analysing(self):
        """Primary entrypoint in the mtriage lifecycle.

        1. Call user-defined `pre_analyse` if it exists.
        2. Read all media from disk.
        3. Call user-defined `analyse_element` in parallel (done through @phase decorator in MTModule). The option
            to bypass parallelisation is for testing.
        4. Call user-defined `post_analyse` if it exists.
        5. Save logs, and clear the buffer."""

        self.logger(
            f"Running analysis {'in parallel' if self.in_parallel else 'serially'}"
        )

        self.__pre_analyse()
        self.__analyse()
        self.__post_analyse()
        cfg = self.get_full_config()
        if not self.errored:
            self.disk.write_meta(
                f"{self.get_selector()}/{self.name}",
                {
                    "etype": self.out_etype.__repr__(),
                    "config": cfg,
                    "stage": {"name": self.name, "module": "analyser"},
                },
            )
        self.flush_logs()

    # INTERNAL METHODS
    @MTModule.phase("pre-analyse")
    def __pre_analyse(self):
        self.pre_analyse(self.config)

    def __analyse(self):
        try:
            elements = self.disk.read_elements(self.config["elements_in"])
            # TODO: check elements from disk match types for what analyser expects
        except:
            raise InvalidAnalyserElements(
                f"The 'elements_in' you specified does not exist on the storage specified."
            )

        if len(elements) == 0:
            raise InvalidAnalyserElements("No elements could be found at the location you tried to select or passed in.")

        if self.in_parallel:
            self.analyse((e for e in elements))
        else:
            # analysing elements as a list will bypass parallelisation
            self.analyse(elements)

    # getter for dest_q. NOTE: abstraction leak from mtmodule parallelisation..
    def get_dest_q(self):
        return self.dest_q.value if self.in_parallel else self.dest_q

    # setter for dest_q. NOTE: abstraction leak from mtmodule parallelisation..
    def set_dest_q(self, value):
        if self.in_parallel:
            self.dest_q.value = value
        else:
            self.dest_q = value

    @MTModule.phase("analyse")
    def analyse(
        self, elements: Union[Generator[LocalElement, None, None], List[LocalElement]]
    ):
        """If `elements` is a Generator, the phase decorator will run in parallel.
        If `elements` is a List, then it will run serially (which is useful for testing)."""
        for element in elements:
            # NB: `super` infra is necessary in case a storage class overwrites
            # the `read_query` method as LocalStorage does.
            og_query = super(type(self.disk), self.disk).read_query(element.query)
            self.set_dest_q(f"{og_query[0]}/{self.name}")

            self.__attempt_analyse(5, element)
            self.disk.delete_local_on_write = False

    def get_selector(self):
        sel = ""
        for q in self.config["elements_in"]:
            selname, _ = super(type(self.disk), self.disk).read_query(q)
            sel += selname
        return sel

    @MTModule.phase("post-analyse")
    def __post_analyse(self):
        # TODO: is there a way to only do this work if overridden?
        analysed_els = self.disk.read_elements([self.get_dest_q()])
        outel = self.post_analyse(analysed_els)
        if outel is None:
            return

        successes = []
        # NOTE: this is duplicated code from `get_selector` in this same class,
        # as it supports `elements_in` as a list. We perhaps need to just
        # enforce `elements_in` as a single query, rather than a list of
        # queries.
        for q in self.config["elements_in"]:
            selname, _ = super(type(self.disk), self.disk).read_query(q)
            success = self.disk.write_element(f"{selname}/{self.name}", outel)
            successes.append(success)

        if not all(successes):
            raise ElementShouldRetryError(
                "Some instances of the final element produced via 'post_analyse' failed to save."
            )

    def __attempt_analyse(self, attempts, element):
        try:
            new_element = self.analyse_element(element, self.config)
            if new_element is None:
                return
            success = self.disk.write_element(self.get_dest_q(), new_element)
            if not success:
                raise ElementShouldRetryError("Unsuccessful storage")

        except ElementShouldSkipError as e:
            self.error_logger(str(e), element)
        except ElementShouldRetryError as e:
            self.error_logger(str(e), element)
            if attempts > 1:
                return self.__attempt_analyse(attempts - 1, element)
            else:
                self.error_logger(
                    "failed after maximum retries - skipping element", element
                )
                self.errored = True
        except Exception as e:
            if self.is_dev():
                raise e
            else:
                self.error_logger(f"{str(e)}: skipping element", element)
                print(traceback.format_exc())

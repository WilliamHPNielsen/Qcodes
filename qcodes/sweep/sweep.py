import itertools
import numpy as np

import qcodes


class ParametersTable:
    def __init__(self, table_list=None, dependent_parameters=None, independent_parameters=None):

        if not any([table_list, dependent_parameters, independent_parameters]):
            raise ValueError("At least one argument should be a non-None")

        dependent_parameters = [] if dependent_parameters is None else dependent_parameters
        independent_parameters = [] if independent_parameters is None else independent_parameters

        if table_list is None:
            self._table_list = [{
                "dependent_parameters": list(dependent_parameters),
                "independent_parameters": list(independent_parameters)
            }]
        else:
            self._table_list = list(table_list)

    def copy(self):
        return ParametersTable(self._table_list)

    def __add__(self, other):
        return ParametersTable(self._table_list + other.table_list)

    def __mul__(self, other):
        return ParametersTable([
            {k: s[k] + o[k] for k in s.keys()} for s, o in itertools.product(self._table_list, other.table_list)
        ])

    def flatten(self):
        ind = {k: v for d in self._table_list for k, v in d["independent_parameters"]}
        dep = {k: v for d in self._table_list for k, v in d["dependent_parameters"]}
        return ind, dep

    def symbols_list(self):
        ind, dep = self.flatten()
        return list(ind.keys()) + list(dep.keys())

    @property
    def table_list(self):
        return self._table_list


def measurement(param_list):
    """
    A decorator to easily integrate arbitrary measurement functions in sweeps.
    """
    def decorator(f):
        def inner():
            m = np.atleast_1d(f())
            return {p[0]: im for p, im in zip(param_list, m)}

        parameter_table = ParametersTable(dependent_parameters=param_list)
        return lambda: (inner, parameter_table)

    return decorator


def setter(param_list):
    """
    A decorator to easily integrate arbitrary setter functions in sweeps
    """
    def decorator(f):
        def inner(value):
            value = np.atleast_1d(value)
            f(*value)
            return {p[0]: im for p, im in zip(param_list, value)}

        parameter_table = ParametersTable(independent_parameters=param_list)
        return lambda: (inner, parameter_table)

    return decorator


def wrap_objects(*objects, repeat=False):

    def wrapper(obj):
        if isinstance(obj, qcodes.Parameter):
            new_obj = ParameterWrapper(obj, repeat=repeat)
        elif isinstance(obj, BaseSweepObject):
            new_obj = obj
        elif callable(obj):
            new_obj = FunctionWrapper(obj, repeat=repeat)
        else:
            raise ValueError("Do not know how to wrap instance of ", type(obj))

        return new_obj

    return [wrapper(obj) for obj in objects]


class BaseSweepObject:
    """
    A sweep object is defined as follows:
    1) It is iterable and looping over a sweep object shall result in independent measurement parameters being set
    at each iteration
    2) At each iteration a dictionary is returned containing information about the parameters set and measurements that
    have been performed.
    """

    def __init__(self):
        # The following attributes are determined when we begin iteration...
        self._param_setter = None  # A "param_setter" is an iterator which, when "next" is called a new value of the
        # independent parameter is set.
        self._parameter_table = None  # A proper parameter table should be defined by the subclasses
        self._symbols_list = None

    def _setter_factory(self):
        """
        When called, this method returns the param setter iterable appropriate for this measurement
        """
        raise NotImplementedError("Please subclass BaseSweepObject")

    def _start_iter(self):
        self._param_setter = self._setter_factory()
        if self._parameter_table is not None:
            self._symbols_list = self._parameter_table.symbols_list()
        else:
            self._symbols_list = []

    def __iter__(self):
        self._start_iter()
        return self

    def __next__(self):
        if self._param_setter is None:
            self._start_iter()

        nxt = next(self._param_setter)
        if len(nxt) and len(self._symbols_list):
            nxt = {k: nxt[k] if k in nxt else None for k in self._symbols_list}

        return nxt

    def __call__(self, *sweep_objects):
        return Nest([self, Chain(wrap_objects(*sweep_objects))])

    @property
    def parameter_table(self):
        return self._parameter_table


class IteratorSweep(BaseSweepObject):
    """
    Sweep independent parameters by unrolling an iterator. This class is useful if we have "bare" parameter set iterator
    and need to create a proper sweep object as defined in the BaseSweepObject docstring. See the "Nest"
    subclass for an example.

    Parameters
    ----------
    iterator_function: callable
        A callable with no parameters, returning an iterator. Unrolling this iterator has the
        effect of setting the independent parameters.
    """
    def __init__(self, iterator_function):
        super().__init__()
        self._iterator_function = iterator_function

    def _setter_factory(self):
        for value in self._iterator_function():
            yield value


class Nest(BaseSweepObject):
    """
    Nest multiple sweep objects. This is for example very useful when performing two or higher dimensional scans
    (e.g. sweep two gate voltages and measure a current at each coordinate (gate1, gate2).

    Notes
    -----
    We produce a nested sweep object of arbitrary depth by first defining a function which nests just two sweep objects

        product = two_product(so1, so2)

    A third order nest is then achieved like so:

        product = two_product(so1, two_product(so2, so3))

    A fourth order by

        product = two_product(so1, two_product(so2, two_product(so3, so4)))

    Etc...
    """

    def __init__(self, sweep_objects):
        super().__init__()
        self._sweep_objects = sweep_objects
        self._parameter_table = np.prod([so.parameter_table for so in sweep_objects])

    @staticmethod
    def _two_product(sweep_object1, sweep_object2):
        """
        Parameters
        ----------
        sweep_object1: BaseSweepObject
        sweep_object2: BaseSweepObject

        Returns
        -------
        BaseSweepObject
        """

        def inner():
            for result2 in sweep_object2:
                for result1 in sweep_object1:
                    result1.update(result2)

                    yield result1

        return IteratorSweep(inner)

    def _setter_factory(self):
        prod = self._sweep_objects[0]
        for so in self._sweep_objects[1:]:
            prod = self._two_product(so, prod)

        return prod


class Chain(BaseSweepObject):
    """
    Chain a list of sweep object to run one after the other
    """
    def __init__(self, sweep_objects):
        super().__init__()
        self._sweep_objects = sweep_objects
        self._parameter_table = np.sum([so.parameter_table for so in sweep_objects])

    def _setter_factory(self):
        for so in self._sweep_objects:
            for result in so:
                yield result


class Zip(BaseSweepObject):
    """
    Zip multiple sweep objects. Unlike a nested sweep, we will produce a 1D sweep
    """
    def __init__(self, sweep_objects):
        super().__init__()
        self._sweep_objects = sweep_objects
        self._parameter_table = np.prod([so.parameter_table for so in sweep_objects])

    @staticmethod
    def _combine_dictionaries(dictionaries):
        combined = {}
        for d in dictionaries:
            combined.update(d)
        return combined

    def _setter_factory(self):
        for results in zip(*self._sweep_objects):
            yield Zip._combine_dictionaries(results)


class ParameterSweep(BaseSweepObject):
    """
    Sweep independent parameters by looping over set point values and setting a QCoDeS parameter to this value at
    each iteration

    Parameters
    ----------
    parameter: qcodes.StandardParameter
    point_function: callable
        Unrolling this iterator returns to us set values of the parameter
    """

    def __init__(self, parameter, point_function):
        super().__init__()
        self._parameter = parameter
        self._point_function = point_function
        self._parameter_table = ParametersTable(independent_parameters=[(parameter.full_name, parameter.unit)])

    def _setter_factory(self):
        for set_value in self._point_function():
            self._parameter.set(set_value)
            yield {self._parameter.full_name: set_value}


class ParameterWrapper(BaseSweepObject):
    """
    A wrapper class which iterates once ans returns the value of a QCoDeS parameter

    Parameters
    ----------
    parameter: qcodes.StandardParameter
    """
    def __init__(self, parameter, repeat=False):
        """
        Note: please use repeat=True carefully as this can easily lead to infinite loops.
        """
        super().__init__()
        self._parameter_table = ParametersTable(dependent_parameters=[(parameter.full_name, parameter.unit)])
        self._parameter = parameter
        self._repeat = repeat

    def _setter_factory(self):
        stop = False
        while not stop:
            value = self._parameter()
            yield {self._parameter.full_name: value}
            stop = not self._repeat


class FunctionSweep(BaseSweepObject):
    """
    Use a function to set independent parameters instead of calling set methods directly
    """
    def __init__(self, set_function, point_function):
        super().__init__()
        self._set_function, self._parameter_table = set_function()
        self._point_function = point_function

    def _setter_factory(self):
        for set_value in self._point_function():
            yield self._set_function(set_value)


class FunctionWrapper(BaseSweepObject):
    """
    Use a function to measure dependent parameters instead of calling get methods directly
    """
    def __init__(self, measure_function, repeat=False):
        """
        Note: please use repeat=True carefully as this can easily lead to infinite loops.
        """
        super().__init__()
        self._measure_function, self._parameter_table = measure_function()
        self._repeat = repeat

    def _setter_factory(self):
        stop = False
        while not stop:
            yield self._measure_function()
            stop = not self._repeat


def sweep(obj, sweep_points):
    """
    A convenience function to create a 1D sweep object

    Parameters
    ----------
    obj: qcodes.StandardParameter or callable
        If callable, it shall be a callable of three parameters: station, namespace, set_value and shall return a
        dictionary
    sweep_points: iterable or callable returning a iterable
        If callable, it shall be a callable of two parameters: station, namespace and shall return an iterable

    Returns
    -------
    FunctionSweep or ParameterSweep
    """

    if not callable(sweep_points):
        point_function = lambda: sweep_points
    else:
        point_function = sweep_points

    if not isinstance(obj, qcodes.Parameter):
        if not callable(obj):
            raise ValueError("The object to sweep over needs to either be a QCoDeS parameter or a function")

        return FunctionSweep(obj, point_function)
    else:
        return ParameterSweep(obj, point_function)


def nest(*objects):
    return Nest(wrap_objects(*objects))


def chain(*objects):
    return Chain(wrap_objects(*objects))


def szip(*objects):
    """
    A plausible scenario for using szip is the following:

    >>> szip(therometer.t, sweep(source.voltage, [0, 1, 2]))

    The idea is to measure the temperature *before* going to each voltage set point. The parameter "thermometer.t" needs
    to be wrapped by the ParameterWrapper in such a way that the get method of the parameter is called repeatedly. An
    infinite loop is prevented because the sweep object "sweep(source.voltage, [0, 1, 2])"  has a finite length and the
    Zip operator loops until the shortest sweep object is exhausted
    """
    repeat = False
    if any([isinstance(i, BaseSweepObject) for i in objects]):  # If any of the objects is a sweep object it (probably)
        # has a finite length and therefore wrapping functions and parameters with repeat=True is save. These parameters
        # and functions will be called as often as the length of the sweep object.
        repeat = True
    return Zip(wrap_objects(*objects, repeat=repeat))
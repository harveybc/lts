As you can see we have some requirements in the REFERNCE_plugins.md that have a clear defined goal, kdevelop a secure modular, extensible live trading system (lts repo).  This repo contains a broker, pipeline, porfolio and strategy pplugins that are described in the reference. I want to test something called behavior driven development mixed with the design in each step.  so we need to perform the design p[hase alongside the test designs  in 4 phases starting atop(acceptance tests , design: user requirements) to botton(unit tests). each of the levels also correspond with a design phase that  has to be described and doumented it its own sepoarate md file detailing the precise design constraints, design decisions and requirements both in structure and behavior:
- acceptance tests: from design of tests based on final user user stories and requirements
- system tests: from the ddesign of tests based on security, and other system constraints like the selected technologies of implementation
- integration tests: from the design of the structure (modules, plugins) and desired behavior of the different componets,  where we can get the required interactions
- unit tests: from the specific module or plugin design, with tests for every desired behavior of the component, remember that here is where the main difference between simple test-driven develompent and behavior-driven development is made, since we need not to test every method or class, but instead we need to test every desired behavior, also remem ber the tests need to test at least one negative test per each positive test, meaning that we must test that the desired behavior is passing, but also that some other or other non-desired behaviors are managed correctly fopr the test to pass.parts of the program since i use the same framework for configuration merging, loading and plugin loading using the built-in python plugin system that uses the setup.py to specify the different plugins.

After the 4 test and architecture design phases (all the code for the tests must be created, but tests still not executed) are corectly implmented and the deocumentation for the master test_plan.md and the tests/unit/plan_unit.md,  tests/integration/plan_integration.md, tests/system/plan_system.md, tests/acceptance/plan_acceptance.md is complete, and also the design document design_acceptance.md, design_system.md, design_integration.md and design_unit.md( list of componets and list of behaviors, every module described, mentioning the filename of the code).After those 4 design phases are ok, we proceed with the next 4 implmentation phases in which we start from bottom to top (unit to acceptance), by creating the python code to make all tests pass.

Remember to use the same exact structure for all plugins:

plugin_params ={"plugin_specific_param_1": default value, ..}
plugin_debug_vars=["debug_variable_or_metric1",..]
def __init__(self):
        self.params = self.plugin_params.copy()

    def set_params(self, **kwargs):
        """
        Actualiza los parámetros del pipeline combinando los parámetros específicos con la configuración global.
        """
        for key, value in kwargs.items():
            self.params[key] = value

    def get_debug_info(self):
        """
        Devuelve información de debug de los parámetros relevantes del pipeline.
        """
        return {var: self.params.get(var) for var in self.plugin_debug_vars}

    def add_debug_info(self, debug_info):
        """
        Agrega la información de debug al diccionario proporcionado.
        """
        debug_info.update(self.get_debug_info())

That structure must be exactly the same, i repeat, exactly the same as in the code i showmn, except for the plugin_params and the plugin_debug_vars that are to be decided by you as the plugin specific parameters with their default values and the debugging or metrics of informative variables to export a s debug info automatically for all plugins, so be sure to include all of that. 


So having all of that into accoount, please create a file called test_plan.md containing the exact description of the current test_plan.md file and directory structure, in a tree format with a little comment in front of each file or folder in the repo. Remeber all the files in the repo ahve skeleton code that may not be updated. so we need to use this skeleton, but create the required files. 

Please start with the README.md, please use the sections of the current one, but please adapt it to ur current project, remember to mention all possible use cases so all the acceptance tests can be made correctly. But after i aprove your version of the README.md, you can start the methodoly i just mentioned. In that way i can describe the exact desired behavior of the app so u can start the aformenmentioned methodology strictly, always using the best practices for everything. use the best practices for software testing and for code development.

Include in the readme, at the end, the section user stories: Wehere ytou must try to write all the possible user stories for ourtr live trading system from the point of view of all the stakeholders(developer that wants to use a new plugin, user that uses a pipeline to create a portafolio using  some strategy for each(may use differnt strategies) asset in the portfolio) to trade on a  broker via its api (implemented as a plugin). Another user that wants to use an already configure lts portfolio to trade with a different broker and using a different set of strategies per asset and a differnt portfolio manager (another portfolio plugin), for now we can do that by l;oading a json config file in the system using the --load_config parameter, but our config merger already supports overrriding the file-loaded config parametes with the . 



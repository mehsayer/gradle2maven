package demo;
import com.google.common.collect.ImmutableList;
import org.slf4j.LoggerFactory;
public class App {
    public static ImmutableList<String> items() { LoggerFactory.getLogger(App.class); return ImmutableList.of("a", "b"); }
}

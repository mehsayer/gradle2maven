package core;
import com.google.common.base.Strings;
import lombok.Value;
@Value
public class Greeter {
    String name;
    public String greet() { return "Hello " + Strings.nullToEmpty(name) + org.apache.commons.io.FilenameUtils.getExtension("a.txt"); }
}
